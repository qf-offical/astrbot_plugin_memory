"""AstrBot 记忆插件入口。 

只挂一个钩子：on_llm_request。设计上的三条硬规矩：

1. **只往 system_prompt 末尾追加，绝不碰 req.contexts。**
   两个理由：
   a) AstrBot 平台自带长期记忆（LTM），它已经往 contexts 里塞了聊天历史。
      我们再塞一份，模型会看到两套互相矛盾的历史记录。
   b) 更严重的是：contexts 会被 _save_to_history 当作真实对话落库
      （只跳过第一条 system）。往里面注入记忆 = 写进会话历史，
      下一轮读回来再插一遍，无限累积，把存储撑爆。

2. **priority=-1，排在其他插件之后执行。**
   AstrBot 的 priority 数值大者先跑，-1 让我们最后执行。这样别的插件
   先注入它们的内容，我们的记忆追加在最末尾；即便整体被截断，最先牺牲
   的也是记忆，不会动到别人的东西。

3. **任何异常都不能阻止回话。**
   记忆是增强功能，不是必需功能。所有对外路径都包 try/except，
   出错只记 warning，然后照常让请求发出去。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star
from astrbot.core.star.star_tools import StarTools

from .memory.autolearn import (
    build_extraction_prompt,
    parse_extraction_result,
    should_skip_text,
)
from .memory.config import load_config
from .memory.layers import MemoryLayers
from .memory.recall import recall

# 后台整理间隔：10 分钟做一次 consolidate + sweep
# 默认维护间隔（秒）。实际用配置项 maintenance_interval_min 覆盖。
DEFAULT_MAINTENANCE_INTERVAL_S = 600


class MemoryPlugin(Star):
    def __init__(self, context, config: dict | None = None) -> None:
        super().__init__(context)
        self.cfg = load_config(config)
        self.layers: MemoryLayers | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        # 在途的后台写入任务。terminate 时要等它们写完，
        # 否则热重载/关停会丢掉当轮记忆。
        self._pending: set[asyncio.Task] = set()

    # ── 生命周期 ────────────────────────────────────────────────────────
    async def initialize(self) -> None:
        if not self.cfg.get("enabled", True):
            logger.info("[mem] 插件已禁用，跳过初始化")
            return

        # 用 AstrBot 的规范数据目录（data/plugin_data/<插件名>/）：
        # 自造目录在插件卸载后不会被清理，会永久残留。
        try:
            base = StarTools.get_data_dir("astrbot_plugin_memory")
        except Exception as e:
            logger.warning(f"[mem] 数据目录不可用，插件不启用：{e}")
            return

        self.layers = MemoryLayers(base, self.cfg)
        self._task = asyncio.create_task(self._maintenance_loop())
        logger.info(f"[mem] 已启用，数据目录 {base}，配置 {self.cfg}")

    async def terminate(self) -> None:
        self._stopping = True

        # 先停维护循环
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

        # 再等后台写入落地：最多等 3 秒，超时就放弃
        # （宁可丢最后一轮记忆，也不能让关停卡住）
        if self._pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._pending, return_exceptions=True),
                    timeout=3.0,
                )
            except (asyncio.TimeoutError, Exception):
                pass
            self._pending.clear()

        logger.info("[mem] 已停用")

    async def _auto_learn(self) -> None:
        """从近期对话里提炼长期记忆。

        流程：
          1. 取各会话最近的中期块（不含刚说过的话，那还在短期层）
          2. 预筛掉寒暄/太短的
          3. 每个会话最多调一次模型，让它返回 JSON 数组
          4. 解析结果写入长期层

        失败一律静默：自动学习是锦上添花，坏了不能影响任何事。
        """
        try:
            provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None:
            # 没配模型时不要每 10 分钟刷一条警告
            return

        try:
            # 按会话分组取最近的中期块
            by_chat: dict[str, list[str]] = {}
            for rec in self.layers.episodic_all():
                cid = str(rec.get("chat_id", ""))
                text = str(rec.get("text", "")).strip()
                if not cid or not text:
                    continue
                by_chat.setdefault(cid, []).append(text)
            if not by_chat:
                return

            learned = 0
            for chat_id, texts in by_chat.items():
                # 只用最近 3 块，越老的相关性越低
                recent = texts[-3:]
                facts_source = [t for t in recent if not should_skip_text(t)]
                if not facts_source:
                    continue

                prompt = build_extraction_prompt(facts_source)
                if not prompt:
                    continue

                try:
                    resp = await asyncio.wait_for(
                        provider.text_chat(
                            prompt=prompt,
                            system_prompt=(
                                "你是一个记忆提取器。只输出 JSON 数组，不要解释。"
                            ),
                        ),
                        timeout=45.0,
                    )
                    raw = getattr(resp, "completion_text", None) or str(resp)
                except asyncio.TimeoutError:
                    logger.warning(f"[mem] 自动提炼超时，跳过 {chat_id}")
                    continue
                except Exception as e:
                    logger.warning(f"[mem] 自动提炼调用失败，跳过 {chat_id}：{e}")
                    continue

                facts = parse_extraction_result(raw)
                for fact in facts:
                    try:
                        key = self.layers.add_semantic(chat_id, fact)
                        if key:
                            learned += 1
                    except Exception:
                        continue

            if learned:
                logger.info(f"[mem] 自动提炼了 {learned} 条长期记忆")

        except Exception as e:
            # 兜底：任何意外都不能让维护循环挂掉
            logger.warning(f"[mem] 自动提炼失败：{e}")

    def _maintain_once(self) -> tuple[int, int]:
        """一次整理：短期转中期 + 清理过期（在线程池里跑）。"""
        moved = self.layers.consolidate() if self.layers else 0
        removed = self.layers.sweep() if self.layers else 0
        return moved, removed

    async def _maintenance_loop(self) -> None:
        """每 10 分钟整理一次：短期转中期、清理过期。"""
        # 间隔可配：调小便于验证，调大省资源
        interval = max(60, int(self.cfg.get("maintenance_interval_min", 10)) * 60)

        while not self._stopping:
            await asyncio.sleep(interval)
            try:
                if not self.layers:
                    continue
                # 放到线程池：6.7MB 时 consolidate 实测 163ms、sweep 64ms，
                # 同步跑会卡住事件循环，机器人的回复能明显感到停顿。
                moved, removed = await asyncio.to_thread(self._maintain_once)
                if moved or removed:
                    logger.info(f"[mem] 整理完成：转中期 {moved} 块，清理 {removed} 条")

                # 自动提炼长期记忆：必须在这里（async 上下文）调模型，
                # 不能塞进 to_thread —— text_chat 本身是协程。
                if self.cfg.get("auto_learn_semantic"):
                    await self._auto_learn()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[mem] 后台整理失败：{e}")

    # ── 钩子 ────────────────────────────────────────────────────────────
    @filter.on_llm_request(priority=-1)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self.cfg.get("enabled", True) or not self.layers:
            return

        try:
            chat_id = self._chat_id(event)
            prompt = str(req.prompt or "")

            # 1) 记下本轮用户输入（写短期）
            #
            # 放进线程池执行：record() 要 read_all + rewrite 整个 working.jsonl，
            # 1000 条记录时约 400KB 同步 IO。直接在 async 钩子里跑会阻塞
            # 事件循环，群聊活跃时能把整个 AstrBot 拖慢。
            # 用 to_thread 扔到线程池，钩子不用等它写完就能继续。
            if prompt.strip():
                try:
                    task = asyncio.create_task(
                        asyncio.to_thread(self.layers.record, chat_id, "user", prompt)
                    )
                    # 必须留引用：不保存的话 CPython 可能在任务完成前回收，
                    # 异常也只会打一句 "Task exception was never retrieved"，
                    # 看不到堆栈。terminate() 还要靠这个集合等它们写完。
                    self._pending.add(task)
                    task.add_done_callback(self._pending.discard)
                except Exception:
                    # 线程池/事件循环不可用时退回同步写（宁可慢一点也不能丢记忆）
                    self.layers.record(chat_id, "user", prompt)

            # 2) 检索中期 + 长期
            candidates = self.layers.all_searchable(chat_id)
            hits = recall(
                prompt,
                candidates,
                top_n=int(self.cfg["recall_top_n"]),
                timeout_s=0.2,
                chat_id=chat_id,          # 会话隔离：不跨群/跨私聊串记忆
            )
            if not hits:
                return

            # 3) 拼注入块，按上限截断
            block = self._render_block(hits)
            if not block:
                return

            # 4) 追加到 system_prompt 末尾（不碰 contexts）
            req.system_prompt = (req.system_prompt or "") + "\n\n" + block
            logger.info(f"[mem] 注入 {len(block)} 字符 / {len(hits)} 条")

            # 5) 命中长期记忆的，计一次访问（用于热度排序）
            #
            # touch 每次都会 read_all + rewrite 整个 semantic.jsonl，
            # 3000 条时 3 次 touch 实测 62ms —— 不能放在 async 关键路径上。
            keys = [str(h["key"]) for h in hits if h.get("role") == "fact" and h.get("key")]
            if keys:
                try:
                    t = asyncio.create_task(asyncio.to_thread(self._touch_many, keys))
                    self._pending.add(t)
                    t.add_done_callback(self._pending.discard)
                except Exception:
                    pass   # 访问计数丢了不影响正确性，静默跳过

        except Exception as e:
            # 记忆不能阻断回话
            logger.warning(f"[mem] 注入失败，已跳过：{e}")

    # ── 长期记忆管理命令 ────────────────────────────────────────────────
    #
    # 为什么需要这个：长期层（semantic）没有自动写入入口，
    # 不提供命令的话它在运行时就永远是空的，三层设计里的「长期」形同虚设。
    #
    # 命令：
    #   /记忆 <内容>    把一句话写进当前会话的长期记忆
    #   /记忆查看       看当前会话记住了多少条
    #   /记忆清空       清空当前会话的长期记忆
    #
    # 这里没做「主人校验」——AstrBot 的白名单机制已经把非授权用户挡在外面。
    # 如需更细的权限控制，可以在配置里加 owner_ids 再比对 event.get_sender_id()。

    @filter.command("记忆")
    async def cmd_memory(self, event: AstrMessageEvent, content: str = ""):
        """记忆管理：/记忆 <内容> | /记忆查看 | /记忆清空"""
        if not self.layers:
            return "记忆插件未启用"

        chat_id = self._chat_id(event)
        cmd = content.strip()

        if cmd in ("", "查看", "status"):
            items = [r for r in self.layers.semantic_all() if r.get("chat_id") == chat_id]
            if not items:
                return f"当前会话还没有长期记忆。（短期 {len(self.layers.working(chat_id))} 条）"
            lines = [f"- {r.get('text', '')}" for r in items[-10:]]
            return f"长期记忆 {len(items)} 条：\n" + "\n".join(lines)

        if cmd in ("清空", "clear", "reset"):
            # 清空是破坏性操作，只允许管理员执行。
            # 不加这道闸，群里任何一个人发「/记忆清空」都能抹掉这个会话
            # 攒下的全部长期记忆 —— 而且没有撤销。
            if not self._is_admin(event):
                return "清空长期记忆需要管理员权限"
            kept = [r for r in self.layers.semantic_all() if r.get("chat_id") != chat_id]
            self.layers.semantic_store.rewrite(kept)
            return "已清空当前会话的长期记忆"

        # 其余情况一律当「要记住的内容」
        try:
            key = self.layers.add_semantic(chat_id, cmd)
            if not key:
                return "内容为空，没有记住"
            return f"记住了：{cmd}"
        except Exception as e:
            return f"记忆写入失败：{e}"

    # ── 辅助 ────────────────────────────────────────────────────────────
    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        """是否管理员。

        AstrBot 的 is_admin() 已经在平台侧做过判定（QQ 官方 bot 看是不是
        管理员，WebChat 看是不是登录用户）。取不到时保守返回 False：
        宁可挡掉一次合法操作，也不能让陌生人清空记忆。
        """
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    @staticmethod
    def _chat_id(event: AstrMessageEvent) -> str:
        """会话标识。私聊/群聊各自独立。"""
        try:
            umo = getattr(event, "unified_msg_origin", None)
            if umo:
                return str(umo)
        except Exception:
            pass
        return "default"

    def _touch_many(self, keys: list[str]) -> None:
        """批量更新访问计数（在线程池里跑）。"""
        for k in keys:
            try:
                self.layers.touch(k)
            except Exception:
                pass

    def _render_block(self, hits: list[dict]) -> str:
        """把命中的记忆渲染成一段文本，限制总长度。"""
        header = str(self.cfg.get("inject_header", "【记忆】"))
        limit = int(self.cfg.get("recall_max_chars", 800))

        lines: list[str] = []
        used = len(header) + 20  # 预留 header 和标题行

        # 保证至少能写进一条：header 很长（用户自定义）而 limit 很小时，
        # 按 limit - used 算出来的预算可能是负的，最后一块记忆都渲染不出来，
        # 整块返回 "" —— 插件看起来在跑，实际完全没注入。
        # 这里给第一条兜底空间，宁可轻微超限，也不让记忆静默失效。
        min_budget = 20

        for idx, h in enumerate(hits):
            text = str(h.get("text", "")).strip()
            if not text:
                continue
            kind = "记得" if h.get("role") == "fact" else "之前聊过"
            prefix = f"- （{kind}）"
            budget = limit - used - len(prefix) - 2

            if idx == 0:
                # 第一条：预算不够就放宽，确保至少有内容
                budget = max(budget, min_budget)
            elif budget < 10:
                # 后续条目空间不够就收手
                break

            # 单条太长时**截断这条**而不是整条丢弃
            if len(text) > budget:
                text = text[:budget] + "…"

            line = prefix + text
            lines.append(line)
            used += len(line) + 1

        if not lines:
            return ""
        return header + "\n" + "\n".join(lines)
