"""三层记忆：短期（working）/ 中期（episodic）/ 长期（semantic）。

职责边界 —— 本模块只管「该留什么、该删什么」，不做检索（那是 recall.py），
也不自己拼 JSON（那是 store.py）。

三层的取舍：
    · 短期   最近 N 条原始对话。每轮都注入，保证连贯，不压缩。
    · 中期   超过 1 小时的短期对话按会话聚合成一条摘要，留 7 天。
             检索命中才注入。
    · 长期   事实与偏好，不过期，但有条数上限（按访问频率淘汰最冷的）。

为什么短期超限是「丢弃」而不是「压缩」：
    压缩需要模型参与，而记录发生在 on_llm_request 里，多一次模型调用
    会把回复延迟拉长。丢弃最老的几条代价更小 —— 反正它们马上会被
    consolidate 搬到中期。
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any

from .store import JsonlStore
from .tokenize import tokenize

# 短期转中期的年龄阈值：1 小时
CONSOLIDATE_AGE_S = 3600


class MemoryLayers:
    def __init__(self, base_dir: Path, cfg: dict[str, Any]) -> None:
        self.base_dir = Path(base_dir)
        self.cfg = cfg
        self.working_store = JsonlStore(self.base_dir, "working.jsonl")
        self.episodic_store = JsonlStore(self.base_dir, "episodic.jsonl")
        self.semantic_store = JsonlStore(self.base_dir, "semantic.jsonl")

        # 跨方法的读-改-写锁。
        #
        # JsonlStore 自己的锁只保证「单次 append / 单次 rewrite」不撕裂，
        # 但 record() 是「read_all → 修改 → rewrite」三步，consolidate()
        # 与它操作同一批文件。两个线程交错执行会互相覆盖，表现为刚记的
        # 话凭空消失。这里用一把实例级锁把整个事务包住。
        #
        # 注意：这是简单的互斥，不做公平性保证。写入本身很快（毫秒级），
        # 争用概率很低；如果将来记录量真的很大，应该换成分片存储。
        self._tx_lock = threading.RLock()

    # ── 短期 ────────────────────────────────────────────────────────────
    def record(
        self,
        chat_id: str,
        role: str,
        text: str,
        ts: float | None = None,
    ) -> None:
        """写一条短期记忆，并立刻按 working_size 截断该会话。"""
        text = (text or "").strip()
        if not text:
            return

        now = time.time() if ts is None else float(ts)
        entry = {
            "chat_id": str(chat_id),
            "role": str(role),
            "text": text[:4000],
            "ts": now,
            "keywords": tokenize(text),
        }

        with self._tx_lock:
            all_records = self.working_store.read_all()
            all_records.append(entry)

            # 按会话分别截断，别的会话不受影响
            size = max(1, int(self.cfg.get("working_size", 20)))
            by_chat: dict[str, list[dict]] = {}
            others: list[dict] = []
            for r in all_records:
                cid = r.get("chat_id")
                if cid == str(chat_id):
                    by_chat.setdefault(cid, []).append(r)
                else:
                    others.append(r)

            kept = by_chat.get(str(chat_id), [])[-size:]

            # 全局兜底：working_size 是「每会话」上限，但文件里存着所有会话。
            # 不加这一层，会话数越多文件越大（500 群时 record 每次要读写 58ms），
            # 而且永不过期。这里超过总条数就按时间淘汰最老的。
            merged = others + kept
            total_cap = size * max(1, int(self.cfg.get("max_sessions", 200)))
            if len(merged) > total_cap:
                merged.sort(key=lambda r: float(r.get("ts", 0)))
                merged = merged[-total_cap:]

            self.working_store.rewrite(merged)

    def working(self, chat_id: str) -> list[dict[str, Any]]:
        cid = str(chat_id)
        return [r for r in self.working_store.read_all() if r.get("chat_id") == cid]

    # ── 中期 ────────────────────────────────────────────────────────────
    def consolidate(self, now: float | None = None) -> int:
        """把超龄的短期记录按会话聚合成中期块，返回新增块数。"""
        with self._tx_lock:
            return self._consolidate_locked(now)

    def _consolidate_locked(self, now: float | None) -> int:
        now = time.time() if now is None else float(now)
        cutoff = now - CONSOLIDATE_AGE_S

        all_working = self.working_store.read_all()
        old = [r for r in all_working if float(r.get("ts", 0)) < cutoff]
        if not old:
            return 0

        fresh = [r for r in all_working if float(r.get("ts", 0)) >= cutoff]

        by_chat: dict[str, list[dict]] = {}
        for r in old:
            by_chat.setdefault(str(r.get("chat_id", "")), []).append(r)

        episodic = self.episodic_store.read_all()
        for cid, recs in by_chat.items():
            recs.sort(key=lambda x: float(x.get("ts", 0)))
            lines = [
                f"{'我' if r.get('role') == 'assistant' else '对方'}: {r.get('text', '')}"
                for r in recs
            ]
            summary = " | ".join(lines)[:2000]
            episodic.append(
                {
                    "chat_id": cid,
                    "role": "summary",
                    "text": summary,
                    # ts 用「搬进中期的时间」，不是原对话时间。
                    #
                    # 用原时间会出事：sweep 按 ts 判断过期，8 天前的对话
                    # 刚被 consolidate 搬进来，ts 还是 8 天前，下一次 sweep
                    # 立刻把它删掉 —— 从来没被检索过就消失了。
                    # 停机超过保留期再启动时，期间所有对话都会这样丢失。
                    "ts": now,
                    # 原对话的时间范围单独留着，展示时用
                    "source_from": float(recs[0].get("ts", now)),
                    "source_to": float(recs[-1].get("ts", now)),
                    "keywords": tokenize(summary),
                    "source_count": len(recs),
                }
            )

        self.episodic_store.rewrite(episodic)
        self.working_store.rewrite(fresh)
        return len(by_chat)

    def episodic_all(self) -> list[dict[str, Any]]:
        return self.episodic_store.read_all()

    # ── 长期 ────────────────────────────────────────────────────────────
    def add_semantic(self, chat_id: str, text: str, ts: float | None = None) -> str:
        """写一条长期事实，返回其 key（用于 touch）。"""
        text = (text or "").strip()
        if not text:
            return ""

        now = time.time() if ts is None else float(ts)
        key = hashlib.sha1(f"{chat_id}|{text}".encode("utf-8")).hexdigest()[:16]
        records = self.semantic_store.read_all()

        # 同一条事实不重复写
        if any(r.get("key") == key for r in records):
            return key

        records.append(
            {
                "key": key,
                "chat_id": str(chat_id),
                "role": "fact",
                "text": text[:2000],
                "ts": now,
                "keywords": tokenize(text),
                "access_count": 0,
                "last_access": 0.0,
            }
        )

        cap = max(1, int(self.cfg.get("semantic_max_entries", 1000)))
        if len(records) > cap:
            # 淘汰「最冷」的，但**优先保住刚写进来的与新近写入的**。
            #
            # 踩过的坑：最初按 (last_access, access_count) 升序取前 cap 条，
            # 新条目的 last_access 也是 0，和旧条目并列，排序不稳定直接把
            # 最新写进去的那条淘汰了 —— 表现为「刚记住的事立刻忘了」。
            #
            # 做法：先挑出「访问次数最少的那些」，并列时淘汰最老的。
            # 保留下来后**重新按时间升序**写回，维持存储的自然时序
            # （读取方按文件顺序遍历，乱序会让最近的记忆排在数组中间）。
            def warmth(r: dict) -> tuple[int, float]:
                return (int(r.get("access_count", 0)), float(r.get("ts", 0)))

            records.sort(key=warmth, reverse=True)
            records = records[:cap]
            records.sort(key=lambda r: float(r.get("ts", 0)))

        self.semantic_store.rewrite(records)
        return key

    def semantic_all(self) -> list[dict[str, Any]]:
        return self.semantic_store.read_all()

    def touch(self, key: str) -> None:
        """把某条长期记忆的访问计数 +1（用于热度排序）。"""
        if not key:
            return
        records = self.semantic_store.read_all()
        changed = False
        for r in records:
            if r.get("key") == key:
                r["access_count"] = int(r.get("access_count", 0)) + 1
                r["last_access"] = time.time()
                changed = True
                break
        if changed:
            self.semantic_store.rewrite(records)

    # ── 清理 ────────────────────────────────────────────────────────────
    def sweep(self, now: float | None = None) -> int:
        """按保留天数清中期、按上限清长期，返回删除条数。"""
        with self._tx_lock:
            return self._sweep_locked(now)

    def _sweep_locked(self, now: float | None) -> int:
        now = time.time() if now is None else float(now)
        days = float(self.cfg.get("episodic_retention_days", 7))
        cutoff = now - days * 86400

        episodic = self.episodic_store.read_all()
        kept = [r for r in episodic if float(r.get("ts", 0)) >= cutoff]
        removed = len(episodic) - len(kept)
        if removed:
            self.episodic_store.rewrite(kept)

        # 中期文件超过轮转阈值时，按「只保留最近一半」重写。
        # store 层提供了 ROTATE_BYTES，但之前没人调用它 —— 是死代码。
        # 中期块本身的保留天数已经能控制大小，这里只是防极端情况
        # （比如每天几千条消息、块特别大）。
        if self.episodic_store.needs_rotate():
            ep = self.episodic_store.read_all()
            half = max(1, len(ep) // 2)
            removed += len(ep) - half
            self.episodic_store.rewrite(ep[-half:])

        sem = self.semantic_store.read_all()
        cap = max(1, int(self.cfg.get("semantic_max_entries", 1000)))
        if len(sem) > cap:
            sem.sort(
                key=lambda r: (float(r.get("last_access", 0)), int(r.get("access_count", 0))),
                reverse=True,
            )
            removed += len(sem) - cap
            self.semantic_store.rewrite(sem[:cap])

        return removed

    # ── 汇总（给 recall 用） ────────────────────────────────────────────
    def all_searchable(self, chat_id: str | None = None) -> list[dict[str, Any]]:
        """检索候选池：短期 + 中期 + 长期。

        为什么要把短期也算进来：
        短期记录要等 1 小时才被 consolidate 搬到中期，在那之前如果只查
        中期+长期，**本轮刚说的话下一轮就问「我刚才说什么」是查不到的**。
        用户会明显感觉到「它转头就忘」。
        """
        pool = self.episodic_all() + self.semantic_all()
        if chat_id is not None:
            pool = pool + self.working(str(chat_id))
        return pool
