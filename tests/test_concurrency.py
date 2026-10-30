"""并发安全：多线程同时 record 不能丢记忆。"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.layers import MemoryLayers  # noqa: E402

CFG = {
    "working_size": 1000,   # 放大，避免正常截断干扰计数
    "episodic_retention_days": 7,
    "semantic_max_entries": 1000,
}


def test_concurrent_record_no_loss(tmp_path):
    """8 线程 × 25 条 = 200 条，一条都不能丢。

    这是 record() 从「同步直写」改成「后台线程 + 事务锁」后的回归保护。
    没有锁时，read-modify-write 交错会让后写的覆盖先写的。
    """
    m = MemoryLayers(tmp_path, CFG)

    def work(n):
        for i in range(25):
            m.record(f"chat{n}", "user", f"消息{n}-{i}")

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = sum(len(m.working(f"chat{n}")) for n in range(8))
    assert total == 200, f"期望 200 条，实际 {total}"


def test_concurrent_record_and_consolidate(tmp_path):
    """一边写一边巩固，不该互相覆盖或抛异常。"""
    m = MemoryLayers(tmp_path, CFG)
    errors = []

    def writer():
        try:
            for i in range(50):
                m.record("c1", "user", f"新消息{i}")
        except Exception as e:
            errors.append(e)

    def consolidator():
        try:
            for _ in range(20):
                m.consolidate()
                m.sweep()
        except Exception as e:
            errors.append(e)

    ts = [threading.Thread(target=writer), threading.Thread(target=consolidator)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert errors == [], f"并发时抛异常：{errors}"
    # 数据没丢没重复：短期 + 中期加起来应覆盖所有写入
    total = len(m.working("c1")) + sum(
        int(e.get("source_count", 0)) for e in m.episodic_all() if e.get("chat_id") == "c1"
    )
    assert total == 50, f"期望 50 条，实际 {total}"
