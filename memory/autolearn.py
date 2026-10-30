"""自动提炼长期记忆：让模型判定哪些话值得长期记住。

为什么放在维护循环里而不是请求路径上：
    每轮都调一次模型判定，等于把每次对话的成本翻倍（多一次 200~500 token
    的请求），而且 QQ 群聊里九成的话不值得长期记。放在 10 分钟一次的
    后台整理里批量做，成本降两个数量级，也不影响回复延迟。

代价：
    新事实最多延迟 10 分钟才进长期层。对「记住这个人是谁」这种需求
    完全够用；对「立刻记住刚才说的」不需要——那由短期层负责。

安全底线（与插件其他部分一致）：
    模型返回垃圾、超时、没配 provider —— 一律静默跳过。
    自动学习失败绝不能让机器人少说一句话。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 单次最多提炼几条。太多了长期层会被日常琐事塞满。
MAX_FACTS_PER_BATCH = 5

# 单条事实最长字数（超出截断）
MAX_FACT_CHARS = 200

# 太短的对话不值得送模型（省一次调用）
MIN_TEXT_CHARS = 6

# 纯寒暄/复读，送给模型也是浪费
_NOISE_PATTERNS = re.compile(
    r"^(在吗|在不在|你好|您好|hi|hello|哈+|嘿+|嗯+|哦+|啊+|呵+|"
    r"早|晚安|早安|好|好的|收到|行|OK|ok|\?+|？+|。+|\.+)$",
    re.IGNORECASE,
)

# 提示词总长上限（防止把一整天的对话全塞进去）
MAX_PROMPT_CHARS = 6000


def should_skip_text(text: str) -> bool:
    """预筛：这段文本是否不值得送给模型判定。

    省调用次数的第一道闸 —— 大部分群聊是「嗯」「在吗」「哈哈哈」，
    这些不需要任何模型参与。
    """
    t = (text or "").strip()
    if not t:
        return True
    if len(t) < MIN_TEXT_CHARS:
        return True
    return bool(_NOISE_PATTERNS.match(t))


def build_extraction_prompt(dialogue: list[str]) -> str:
    """把一批对话拼成「请提取值得长期记住的事实」的提示词。

    返回空字符串表示没有可提炼的内容（调用方据此跳过模型调用）。
    """
    lines = [str(x).strip() for x in dialogue if str(x).strip()]
    if not lines:
        return ""

    body = "\n".join(f"- {line}" for line in lines)
    if len(body) > MAX_PROMPT_CHARS:
        body = body[:MAX_PROMPT_CHARS] + "\n…（后略）"

    return (
        "下面是一段聊天记录。请提取其中**值得长期记住**的事实。\n\n"
        f"{body}\n\n"
        "只提取这类内容：\n"
        "- 关于某人的稳定信息：名字、称呼、身份、喜好、厌恶、习惯\n"
        "- 明确的约定或计划：约好做什么、什么时候\n"
        "- 长期有效的偏好：喜欢/讨厌什么、怎么称呼\n\n"
        "不要提取：\n"
        "- 一次性的闲聊、情绪、玩笑、寒暄\n"
        "- 时间敏感的临时状态（今天累、现在忙）\n"
        "- 从上下文看不出长期价值的细节\n\n"
        f"最多 {MAX_FACTS_PER_BATCH} 条，每条不超过 {MAX_FACT_CHARS} 字。\n"
        "用第三人称陈述，例如「主人养了一只猫叫团子」。\n"
        "如果没有值得记的内容，返回空数组。\n\n"
        "只输出 JSON 数组，不要任何解释：\n"
        '["事实1", "事实2"]'
    )


def parse_extraction_result(raw: Any) -> list[str]:
    """解析模型输出，返回清洗后的事实列表。

    模型经常不听话：包 ```json 代码块、加前言、返回对象而不是数组。
    这里全部容忍；实在解析不出来就返回空列表（宁可不记，也不写垃圾）。
    """
    if not raw:
        return []

    text = str(raw).strip()

    # 剥掉 ```json ... ``` 代码块
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    # 直接解析
    data = None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # 退一步：从文本里抠出第一个 [...] 片段
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except (json.JSONDecodeError, ValueError):
                return []

    if not isinstance(data, list):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, str):
            continue
        fact = item.strip()
        if not fact:
            continue
        if len(fact) > MAX_FACT_CHARS:
            fact = fact[:MAX_FACT_CHARS]
        if fact in seen:
            continue
        seen.add(fact)
        out.append(fact)
        if len(out) >= MAX_FACTS_PER_BATCH:
            break

    return out
