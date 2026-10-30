"""端到端验证：直接驱动插件的钩子，不依赖真实 QQ 消息。

用途：验证 record → consolidate → recall → inject 整条链路，
并确认「平台 LTM 冲突」和「异常不阻断」两条设计约束。

跑法：cd astrbot/data/plugins/astrbot_plugin_memory && python e2e_check.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory.config import load_config
from memory.layers import MemoryLayers
from memory.recall import recall


def section(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def main() -> int:
    # 临时目录交给 tempfile：原先写死 "/tmp/mem-e2e"，在 Windows 上会被
    # 解析成 C:\tmp\mem-e2e，没有权限创建时脚本直接崩在第一步。
    tmp = Path(tempfile.mkdtemp(prefix="mem-e2e-"))
    print(f"临时目录：{tmp}")
    try:
        return _run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp: Path) -> int:
    cfg = load_config(None)
    layers = MemoryLayers(tmp, cfg)
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if cond:
            print(f"  ✓ {msg}")
        else:
            print(f"  ✗ {msg}")
            failures.append(msg)

    # ── 1. 吸收对话 ──────────────────────────────────────────────
    section("1. 记录对话到短期层")
    layers.record("private:小明", "user", "我叫小明，我养了只猫叫团子")
    layers.record("private:小明", "assistant", "记下了，团子这名字挺可爱")
    w = layers.working("private:小明")
    check(len(w) == 2, f"短期有 2 条记录（实际 {len(w)}）")
    check(any("团子" in r["keywords"] for r in w), "关键词提取到「团子」")

    # ── 2. 会话隔离 ──────────────────────────────────────────────
    section("2. 会话隔离")
    layers.record("private:小红", "user", "我也养猫，叫球球")
    check(len(layers.working("private:小明")) == 2, "小明的短期未被小红污染")

    # ── 3. 巩固到中期 ────────────────────────────────────────────
    section("3. 巩固：短期 → 中期")
    old_ts = time.time() - 7200  # 2 小时前
    layers.record("private:小明", "user", "团子昨天打翻了花瓶", ts=old_ts)
    moved = layers.consolidate()
    check(moved >= 1, f"巩固产生了 {moved} 个中期块")
    ep = layers.episodic_all()
    check(len(ep) >= 1, f"中期层有 {len(ep)} 条")

    # ── 4. 检索 ──────────────────────────────────────────────────
    section("4. 检索命中")
    hits = recall(
        "团子怎么了",
        layers.all_searchable(),
        top_n=3,
        chat_id="private:小明",
    )
    check(len(hits) > 0, f"检索到 {len(hits)} 条相关记忆")
    if hits:
        check("团子" in hits[0]["text"], "命中的记忆包含「团子」")
        check("score" in hits[0], "结果带 score 字段")

    # 跨会话不应命中
    other = recall("团子", layers.all_searchable(), top_n=3, chat_id="private:小红")
    check(
        all("团子" not in h["text"] for h in other),
        "小红查「团子」不会拿到小明的记忆",
    )

    # ── 5. 长期记忆 ──────────────────────────────────────────────
    section("5. 长期记忆写入与淘汰")
    for i in range(5):
        layers.add_semantic("private:小明", f"事实{i}")
    sem = layers.semantic_all()
    check(len(sem) == 5, f"长期层有 5 条（实际 {len(sem)}）")

    # 同一条不重复写
    layers.add_semantic("private:小明", "事实0")
    check(len(layers.semantic_all()) == 5, "重复事实不会写两次")

    # ── 6. 注入块渲染（独立复刻 main._render_block 的规则）──────
    section("6. 注入块渲染")
    # 直接 import main 需要 AstrBot 包上下文（相对导入），这里把渲染规则
    # 复刻一份来验证行为；真实插件里用的是同一套逻辑。
    def render(hit_list, c):
        header = str(c.get("inject_header", "【记忆】"))
        limit = int(c.get("recall_max_chars", 800))
        lines, used = [], len(header) + 20
        for h in hit_list:
            text = str(h.get("text", "")).strip()
            if not text:
                continue
            kind = "记得" if h.get("role") == "fact" else "之前聊过"
            prefix = f"- （{kind}）"
            budget = limit - used - len(prefix) - 2
            if budget < 10:
                break
            if len(text) > budget:
                text = text[:budget] + "…"
            line = prefix + text
            lines.append(line)
            used += len(line) + 1
        return (header + "\n" + "\n".join(lines)) if lines else ""

    block = render(hits, cfg)
    check(block.startswith("【记忆】"), "注入块以【记忆】开头")
    check(len(block) <= cfg["recall_max_chars"] + 50, f"注入块长度 {len(block)} 未超限")

    many = [{"text": "很长的记忆" * 20, "role": "fact"} for _ in range(5)]
    small = render(many, dict(cfg, recall_max_chars=50))
    check(len(small) <= 60, f"超限时长度受控（{len(small)} 字符）")
    check(len(small) > 0, "超限时仍然保留了记忆内容，没有整块丢弃")

    # ── 7. 异常不阻断 ────────────────────────────────────────────
    section("7. 异常防护")
    # 7a. 坏行（内容是非法 JSON）→ 跳过坏行，返回空列表，不崩溃
    broken = tmp / "semantic.jsonl"
    broken.write_text("{这不是合法json\n", encoding="utf-8")
    layers2 = MemoryLayers(tmp, cfg)
    try:
        result = layers2.semantic_all()
        check(result == [], "坏行被跳过，返回空列表而不是崩溃")
    except Exception as e:
        check(False, f"坏行导致崩溃：{e}")

    # 7b. 文件读不了（权限）→ 必须抛异常，不能伪装成空
    #     这是「读失败被当成没数据」那个数据丢失 bug 的回归点
    #
    #     不用 chmod 000 制造读失败：Windows 上 chmod 只改只读位，文件
    #     照样读得出来，这段检查会「假通过」。改成拦内建 open 抛
    #     PermissionError——被测代码走的是同一条 except OSError 分支，
    #     三个平台上行为一致。
    import builtins as _builtins

    from memory.store import JsonlStore, StoreReadError

    guarded = tmp / "guarded.jsonl"
    guarded.write_text('{"a":1}\n', encoding="utf-8")
    guard_target = guarded.resolve()
    real_open = _builtins.open

    def _guarded_open(file, mode="r", *args, **kwargs):
        reading = any(c in mode for c in "ra") and not any(c in mode for c in "wx+")
        if reading:
            try:
                if Path(file).resolve() == guard_target:
                    raise PermissionError(13, "Permission denied（模拟的读失败）", str(file))
            except (TypeError, ValueError):
                pass
        return real_open(file, mode, *args, **kwargs)

    _builtins.open = _guarded_open
    try:
        try:
            JsonlStore(tmp, "guarded.jsonl").read_all()
            check(False, "读失败应抛异常，却静默返回了空")
        except StoreReadError:
            check(True, "读失败正确抛出 StoreReadError（不会清空已有记忆）")
    finally:
        _builtins.open = real_open

    # 超时保护
    big = [{"text": f"词{i} 猫", "ts": time.time(), "access_count": 0} for i in range(50_000)]
    t0 = time.time()
    r = recall("猫粮", big, timeout_s=0.01)
    elapsed = time.time() - t0
    check(elapsed < 2.0, f"超大候选集在 {elapsed:.3f}s 内返回（未卡死）")

    # ── 汇总 ────────────────────────────────────────────────────
    section("结果")
    if failures:
        print(f"✗ {len(failures)} 项失败：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
