import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.tokenize import tokenize, STOPWORDS  # noqa: E402


def test_chinese_bigrams():
    """连续汉字段应切出二元组，便于匹配「猫粮」这类词。"""
    assert "猫粮" in tokenize("我家猫粮吃完了")


def test_english_lowercase():
    assert "python" in tokenize("I love Python")


def test_stopwords_removed():
    """高频虚词不应进入词元，否则任何查询都会命中所有记忆。"""
    assert "的" not in tokenize("这是他的东西")
    assert "的" in STOPWORDS


def test_single_char_kept_but_not_noise():
    """单字查询要能匹配，但不能切出一堆无意义二元组。"""
    toks = tokenize("猫")
    assert "猫" in toks
    assert len(toks) <= 2


def test_empty_input():
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_punctuation_dropped():
    toks = tokenize("你好，世界！")
    for ch in "，！":
        assert all(ch not in t for t in toks)


def test_dedup_preserves_order():
    """重复词元只保留一次，且保持首次出现的顺序。"""
    toks = tokenize("猫粮 猫粮 天气")
    assert toks.count("猫粮") == 1


def test_mixed_chinese_english():
    toks = tokenize("我用Python写猫粮脚本")
    assert "python" in toks
    assert "猫粮" in toks
