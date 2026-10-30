import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.layers import MemoryLayers  # noqa: E402

CFG = {
    "working_size": 3,
    "episodic_retention_days": 7,
    "semantic_max_entries": 1000,
}


def mk(tmp_path):
    return MemoryLayers(tmp_path, CFG)


def test_record_and_read_working(tmp_path):
    m = mk(tmp_path)
    m.record("c1", "user", "你好")
    m.record("c1", "assistant", "在的")
    assert len(m.working("c1")) == 2
    assert m.working("c1")[0]["role"] == "user"


def test_working_isolated_per_chat(tmp_path):
    """不同会话的短期记忆必须隔离，否则群聊会串到私聊。"""
    m = mk(tmp_path)
    m.record("c1", "user", "A")
    m.record("c2", "user", "B")
    assert m.working("c1")[0]["text"] == "A"
    assert m.working("c2")[0]["text"] == "B"
    assert len(m.working("c1")) == 1


def test_working_size_capped(tmp_path):
    m = mk(tmp_path)
    for i in range(10):
        m.record("c1", "user", f"m{i}")
    w = m.working("c1")
    assert len(w) == 3
    assert w[-1]["text"] == "m9"


def test_record_extracts_keywords(tmp_path):
    m = mk(tmp_path)
    m.record("c1", "user", "我家猫粮吃完了")
    assert "猫粮" in m.working("c1")[0]["keywords"]


def test_consolidate_moves_old_to_episodic(tmp_path):
    m = mk(tmp_path)
    m.record("c1", "user", "两小时前的话", ts=time.time() - 7200)
    added = m.consolidate()
    assert added >= 1
    assert m.working("c1") == []
    assert len(m.episodic_all()) >= 1


def test_consolidate_keeps_recent(tmp_path):
    m = mk(tmp_path)
    m.record("c1", "user", "刚刚说的")
    assert m.consolidate() == 0
    assert len(m.working("c1")) == 1


def test_consolidate_groups_by_chat(tmp_path):
    """两条不同会话的旧消息应各自成块，不能混在一起。"""
    m = mk(tmp_path)
    m.record("c1", "user", "会话一", ts=time.time() - 7200)
    m.record("c2", "user", "会话二", ts=time.time() - 7200)
    assert m.consolidate() == 2
    chats = {e["chat_id"] for e in m.episodic_all()}
    assert chats == {"c1", "c2"}


def test_sweep_removes_expired_episodic(tmp_path):
    """中期块确实过期（搬入时间超过保留天数）时才删。

    注意：不能用「原对话很老」来构造，那测的是旧 bug 的行为。
    现在 ts 是搬入时间，要让块过期必须把时钟往前推。
    """
    m = mk(tmp_path)
    m.record("c1", "user", "内容", ts=time.time() - 7200)
    m.consolidate()

    # 把 now 推到 10 天之后，此时中期块才真正过期
    future = time.time() + 86400 * 10
    assert m.sweep(now=future) >= 1
    assert m.episodic_all() == []


def test_sweep_keeps_fresh_episodic(tmp_path):
    m = mk(tmp_path)
    m.record("c1", "user", "昨天的", ts=time.time() - 7200)
    m.consolidate()
    assert m.sweep() == 0
    assert len(m.episodic_all()) == 1


def test_semantic_add_and_cap(tmp_path):
    cfg = dict(CFG, semantic_max_entries=3)
    m = MemoryLayers(tmp_path, cfg)
    for i in range(6):
        m.add_semantic("c1", f"事实{i}")
    assert len(m.semantic_all()) == 3
    # 保留最新的三条
    assert m.semantic_all()[-1]["text"] == "事实5"


def test_touch_increments_access(tmp_path):
    m = mk(tmp_path)
    m.add_semantic("c1", "喜欢猫")
    key = m.semantic_all()[0]["key"]
    m.touch(key)
    assert m.semantic_all()[0]["access_count"] == 1


def test_corrupted_store_does_not_crash(tmp_path):
    (tmp_path / "semantic.jsonl").write_text("{bad json\n", encoding="utf-8")
    m = mk(tmp_path)
    assert m.semantic_all() == []


def test_old_conversation_not_immediately_swept(tmp_path):
    """8 天前的对话搬进中期后，不该被紧接着的 sweep 立刻删掉。

    这是审查发现的 bug：中期块的 ts 曾用原对话时间，
    consolidate 刚搬完，sweep 就按过期规则把它删了，
    记忆从来没被检索过就消失。
    """
    m = mk(tmp_path)
    old_ts = time.time() - 86400 * 8
    m.record("c1", "user", "八天前说过的话", ts=old_ts)
    assert m.consolidate() == 1

    ep = m.episodic_all()
    assert len(ep) == 1
    assert ep[0]["source_from"] == old_ts   # 原时间被记录下来

    # 立刻 sweep，不该删
    assert m.sweep() == 0
    assert len(m.episodic_all()) == 1


def test_semantic_write_and_read_by_chat(tmp_path):
    """长期层要能被命令写入，并按会话隔离。"""
    m = mk(tmp_path)
    m.add_semantic("c1", "主人喜欢猫")
    m.add_semantic("c2", "另一个人喜欢狗")

    c1 = [r for r in m.semantic_all() if r["chat_id"] == "c1"]
    assert len(c1) == 1
    assert c1[0]["text"] == "主人喜欢猫"
