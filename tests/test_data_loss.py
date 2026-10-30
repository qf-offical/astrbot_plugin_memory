"""数据丢失防护：读失败绝不能清空已有记忆。

这是审查发现的阻断性 bug 的回归保护：
read_all() 遇 OSError 原本静默返回 []，record() 拿它当基准重写整个文件，
一次权限故障就把全部历史抹掉。

注：读失败用 conftest 的 `fail_read` 模拟，而不是 chmod 000 —— Windows 上
chmod 只改只读位，文件仍然可读，测试会变成「永远通过」的假阳性。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.layers import MemoryLayers  # noqa: E402
from memory.store import JsonlStore, StoreReadError  # noqa: E402

CFG = {
    "working_size": 100,
    "episodic_retention_days": 7,
    "semantic_max_entries": 1000,
}


def test_read_failure_raises_not_empty(tmp_path, fail_read):
    """读失败必须抛异常，不能伪装成「没有数据」。"""
    f = tmp_path / "x.jsonl"
    f.write_text('{"a":1}\n', encoding="utf-8")

    fail_read(f)
    s = JsonlStore(tmp_path, "x.jsonl")
    with pytest.raises(StoreReadError):
        s.read_all()


def test_record_aborts_on_read_failure(tmp_path, fail_read):
    """核心场景：读失败时 record 必须中止，原有记忆一条都不能少。"""
    m = MemoryLayers(tmp_path, CFG)
    for chat in ("c1", "c2", "c3"):
        for i in range(3):
            m.record(chat, "user", f"{chat}-消息{i}")

    before = sum(len(m.working(c)) for c in ("c1", "c2", "c3"))
    assert before == 9

    fail_read(tmp_path / "working.jsonl")
    with pytest.raises(StoreReadError):
        m.record("c1", "user", "故障期间的新消息")

    # 恢复读取再从磁盘上数：关键断言是原有 9 条一条不少。
    # 恢复后回读而不是拿内存里的值，才能证明「文件没被清空」。
    fail_read.restore()
    after = sum(len(m.working(c)) for c in ("c1", "c2", "c3"))
    assert after == before == 9, f"读失败导致记忆丢失：{before} → {after}"


def test_missing_file_is_not_an_error(tmp_path):
    """文件不存在是正常情况（首次运行），应返回空而不是抛异常。"""
    s = JsonlStore(tmp_path, "never_created.jsonl")
    assert s.read_all() == []
