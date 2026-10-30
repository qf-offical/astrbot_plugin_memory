"""中文分词：字符二元组 + 英文小写化 + 停用词过滤。

为什么不用 jieba 之类的分词库：
    AstrBot 插件要能直接丢进 plugins/ 就工作，多一个第三方依赖就多一步
    装包，用户装不上的概率不小。字符二元组对「猫粮」「天气」这类双字词
    命中率够用，对长词的召回差一些，但第一版可以接受。

设计取舍：
    · 中文切二元组而非单字 —— 单字会让「猫」命中所有含猫的句子，精度太差
    · 单字段（整段只有一个字）保留单字本身 —— 否则「猫」这个查询什么都搜不到
    · 英文/数字按连续字母数字切，转小写
    · 停用词过滤掉高频虚词，避免「的」这种字让所有记忆都命中
"""

from __future__ import annotations

import re

# 高频虚词与代词。这些字如果进词元，任何查询都会与所有记忆重合，
# 打分退化成「全部命中」，检索等于失效。
STOPWORDS: set[str] = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "们",
    "和", "与", "就", "都", "也", "很", "还", "把", "被", "让",
    "这", "那", "有", "没有", "不", "吗", "呢", "吧", "啊", "呀",
    "一个", "什么", "怎么", "可以", "这个", "那个",
    "the", "a", "an", "is", "are", "was", "were", "be", "to",
    "of", "and", "or", "in", "on", "at", "it", "this", "that",
}

# 连续汉字 / 连续英文数字 / 其它（丢弃）
_PATTERN = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+")

_MAX_TOKENS = 200  # 单条消息的硬上限，防超长文本把索引撑爆


def tokenize(text: str) -> list[str]:
    """把文本切成去重后的词元列表，保持首次出现顺序。

    中文 → 字符二元组（整段只有一个字时保留该字）
    英文数字 → 小写整词
    停用词 → 丢弃
    """
    if not text or not str(text).strip():
        return []

    out: list[str] = []
    seen: set[str] = set()

    def push(tok: str) -> None:
        if not tok or tok in seen or tok in STOPWORDS:
            return
        seen.add(tok)
        out.append(tok)

    for chunk in _PATTERN.findall(str(text)):
        if chunk.isascii():
            push(chunk.lower())
            continue

        # 中文段
        if len(chunk) == 1:
            push(chunk)
            continue
        for i in range(len(chunk) - 1):
            push(chunk[i : i + 2])
            if len(out) >= _MAX_TOKENS:
                return out

    return out[:_MAX_TOKENS]
