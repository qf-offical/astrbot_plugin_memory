import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.recall import recall  # noqa: E402

NOW = 1_800_000_000.0


def mk(text, age_h=1, freq=0):
    return {"text": text, "ts": NOW - age_h * 3600, "access_count": freq}


def test_keyword_match_scores_higher():
    cands = [mk("我家猫粮吃完了"), mk("今天天气不错")]
    r = recall("猫粮", cands, now=NOW)
    assert r, "应至少命中一条"
    assert r[0]["text"] == "我家猫粮吃完了"


def test_recency_beats_old_with_same_keyword():
    cands = [mk("猫粮", age_h=1), mk("猫粮", age_h=100)]
    r = recall("猫粮", cands, now=NOW)
    assert r[0]["ts"] > r[1]["ts"]


def test_no_match_returns_empty():
    assert recall("量子力学", [mk("猫粮"), mk("天气")], now=NOW) == []


def test_top_n_limits_results():
    cands = [mk(f"猫粮{i}") for i in range(10)]
    assert len(recall("猫粮", cands, top_n=2, now=NOW)) == 2


def test_single_char_query_precision():
    """查「狗」不该把「猫」「天气」也捞回来。"""
    cands = [mk("猫"), mk("狗"), mk("天气不错")]
    r = recall("狗", cands, now=NOW)
    assert len(r) == 1
    assert r[0]["text"] == "狗"


def test_frequency_breaks_tie():
    cands = [mk("猫粮", age_h=5, freq=0), mk("猫粮", age_h=5, freq=9)]
    r = recall("猫粮", cands, now=NOW)
    assert r[0]["access_count"] == 9


def test_empty_query_returns_empty():
    """空查询不能召回全部记忆 —— 那会把提示词塞爆。"""
    assert recall("", [mk("猫粮")], now=NOW) == []
    assert recall("   ", [mk("猫粮")], now=NOW) == []


def test_score_field_present_and_sorted():
    r = recall("猫粮", [mk("猫粮"), mk("猫粮吃完了")], now=NOW)
    assert all("score" in x for x in r)
    assert r[0]["score"] >= r[1]["score"]


def test_does_not_mutate_input():
    cands = [mk("猫粮")]
    recall("猫粮", cands, now=NOW)
    assert "score" not in cands[0]


def test_uses_precomputed_keywords():
    """候选自带 keywords 时直接用，不重复分词。"""
    cand = {"text": "无关文本", "ts": NOW, "access_count": 0, "keywords": ["猫粮"]}
    r = recall("猫粮", [cand], now=NOW)
    assert len(r) == 1


def test_very_old_item_still_recalled_if_keyword_strong():
    """时间衰减不能让关键词强的旧记忆完全消失。"""
    r = recall("猫粮", [mk("猫粮", age_h=1000)], now=NOW)
    assert len(r) == 1


def test_non_dict_items_skipped():
    r = recall("猫粮", [mk("猫粮"), "不是字典", None], now=NOW)
    assert len(r) == 1


def test_chat_isolation_filters_other_chats():
    """A 群的记忆不能注入到 B 群。"""
    a = dict(mk("猫粮"), chat_id="groupA")
    b = dict(mk("猫粮"), chat_id="groupB")
    r = recall("猫粮", [a, b], now=NOW, chat_id="groupA")
    assert len(r) == 1
    assert r[0]["chat_id"] == "groupA"


def test_chat_isolation_allows_missing_chat_id():
    """没有 chat_id 字段的旧数据不应被一刀切丢掉。"""
    no_id = mk("猫粮")
    r = recall("猫粮", [no_id], now=NOW, chat_id="groupA")
    assert len(r) == 1


def test_no_chat_filter_searches_all():
    a = dict(mk("猫粮"), chat_id="groupA")
    b = dict(mk("猫粮"), chat_id="groupB")
    assert len(recall("猫粮", [a, b], now=NOW)) == 2


def test_short_term_searchable_before_consolidate():
    """刚说的话，下一轮就该能检索到，不用等 1 小时巩固。"""
    import tempfile
    from pathlib import Path as _P

    from memory.layers import MemoryLayers

    with tempfile.TemporaryDirectory() as td:
        m = MemoryLayers(_P(td), {"working_size": 20, "episodic_retention_days": 7, "semantic_max_entries": 100})
        m.record("c1", "user", "我刚说我养了只猫叫团子")

        pool = m.all_searchable("c1")
        hits = recall("团子叫什么", pool, now=NOW, chat_id="c1")
        assert len(hits) > 0, "短期记忆应该可检索"
