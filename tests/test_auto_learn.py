"""自动提炼长期记忆（auto_learn_semantic）。

设计要点：
- 在后台维护循环里批量跑，不在请求路径上
- 用模型判定「哪些话值得长期记住」，返回结构化 JSON
- 模型返回垃圾/超时/没配置 provider 时，静默跳过，不能影响任何事
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.autolearn import (  # noqa: E402
    build_extraction_prompt,
    parse_extraction_result,
    should_skip_text,
)


# ── 解析模型输出 ──────────────────────────────────────────────────────
def test_parse_plain_json_array():
    raw = '["主人养了只猫叫团子", "主人叫小明"]'
    assert parse_extraction_result(raw) == ["主人养了只猫叫团子", "主人叫小明"]


def test_parse_json_in_code_fence():
    """模型经常把 JSON 包在 ```json 里。"""
    raw = '```json\n["主人喜欢猫"]\n```'
    assert parse_extraction_result(raw) == ["主人喜欢猫"]


def test_parse_json_with_prefix_text():
    raw = '好的，我提取到：["主人养猫"]'
    assert parse_extraction_result(raw) == ["主人养猫"]


def test_parse_empty_array():
    assert parse_extraction_result("[]") == []


def test_parse_garbage_returns_empty():
    """模型胡言乱语时不能崩，也不能写入垃圾记忆。"""
    for bad in ["我不知道", "", None, "{不是数组}", "[{broken}]"]:
        assert parse_extraction_result(bad) == []


def test_parse_filters_empty_and_non_string():
    raw = '["正常内容", "", null, 123, "   "]'
    assert parse_extraction_result(raw) == ["正常内容"]


def test_parse_dedupes():
    raw = '["同一条", "同一条", "另一条"]'
    assert parse_extraction_result(raw) == ["同一条", "另一条"]


def test_parse_limits_count():
    raw = "[" + ",".join(f'"事实{i}"' for i in range(20)) + "]"
    assert len(parse_extraction_result(raw)) <= 5


def test_parse_limits_length():
    raw = '["' + "很长的内容" * 200 + '"]'
    out = parse_extraction_result(raw)
    assert len(out) == 1
    assert len(out[0]) <= 200


# ── 预筛：不值得送模型的对话 ──────────────────────────────────────────
def test_skip_too_short():
    assert should_skip_text("嗯") is True
    assert should_skip_text("好") is True


def test_skip_pure_greeting():
    assert should_skip_text("在吗") is True
    assert should_skip_text("哈哈哈哈哈") is True


def test_keep_meaningful():
    assert should_skip_text("我叫小明，养了只猫叫团子") is False
    assert should_skip_text("我的生日是三月十五号") is False


def test_skip_empty():
    assert should_skip_text("") is True
    assert should_skip_text("   ") is True


# ── 提示词构造 ────────────────────────────────────────────────────────
def test_prompt_contains_dialogue():
    p = build_extraction_prompt(["我叫小明", "我养了只猫叫团子"])
    assert "小明" in p
    assert "团子" in p


def test_prompt_asks_for_json():
    p = build_extraction_prompt(["测试"])
    assert "JSON" in p or "json" in p


def test_prompt_empty_dialogue():
    assert build_extraction_prompt([]) == ""


def test_prompt_truncates_huge_input():
    """超长对话要截断，不能把提示词撑爆。"""
    big = ["很长的内容" * 100 for _ in range(200)]
    p = build_extraction_prompt(big)
    assert len(p) < 8000
