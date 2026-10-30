"""检索：关键词重合 + 时间衰减 + 访问频率，三因子打分。

为什么不走向量检索：
    embedding 要么调 API（每条记忆都要花钱），要么本地跑模型（多几百 MB
    依赖）。第一版要验证的是「记忆有没有用」，不是「检索有多准」。
    字符二元组 + 时间衰减在群聊这种短句场景下够用。

打分公式：
    score = 0.6 * overlap + 0.3 * decay + 0.1 * freq
        overlap = 命中词元数 / 查询词元数        （关键词贴合度）
        decay   = exp(-age_hours*ln2 / 36)       （36 小时半衰）
        freq    = min(1, access_count / 10)      （常用记忆加权）

为什么 decay 权重只有 0.3：
    试过 0.5，结果「三天前提到的猫」输给「刚才提到的天气」——关键词明明
    更贴，却因为时间新而排前面。0.3 让关键词占主导，时间只做微调。
"""

from __future__ import annotations

import math
import time
from typing import Any

from .tokenize import tokenize

W_OVERLAP = 0.6
W_DECAY = 0.3
W_FREQ = 0.1

# 36 小时半衰：差不多是「隔天还记得」的量级。
# 注意公式是 exp(-age * ln2 / halflife)：直接用 exp(-age/36) 的实际半衰期
# 是 36*ln2 ≈ 25 小时，不是 36 小时 —— 这是容易写错的点。
DECAY_HALFLIFE_H = 36.0
_LN2 = math.log(2.0)

# 默认超时。20 万条候选在 0.05s 内跑不完就直接放弃 ——
# 记忆检索不该拖慢回复。
DEFAULT_TIMEOUT_S = 0.2

# 每处理多少条检查一次超时（每次调 time.time() 也有成本）
_TIMEOUT_CHECK_EVERY = 500


def recall(
    query: str,
    candidates: list[dict[str, Any]],
    top_n: int = 3,
    now: float | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    chat_id: str | None = None,
) -> list[dict[str, Any]]:
    """从候选记忆里挑出与 query 最相关的 top_n 条。

    返回**浅拷贝**（不修改入参），每条多一个 score 字段，按 score 降序。
    超时或异常一律返回空列表 —— 记忆检索失败不能影响正常回复。
    """
    if not candidates:
        return []

    q_tokens = set(tokenize(query))
    if not q_tokens:
        # 空查询（含全是停用词）不能召回全部
        return []

    now = time.time() if now is None else float(now)
    start = time.time()
    scored: list[dict[str, Any]] = []

    try:
        for idx, item in enumerate(candidates):
            if idx % _TIMEOUT_CHECK_EVERY == 0 and idx and (time.time() - start) > timeout_s:
                return []

            if not isinstance(item, dict):
                continue

            # 会话隔离：指定了 chat_id 就只看这个会话的记忆。
            # 不隔离的话，A 群发生过的事会被注入到 B 群，既不准也有隐私问题。
            if chat_id is not None and item.get("chat_id") not in (chat_id, None):
                continue

            text = str(item.get("text", ""))
            # 候选自带 keywords 时直接用，避免重复分词
            kw = item.get("keywords")
            c_tokens = set(kw) if isinstance(kw, list) and kw else set(tokenize(text))
            if not c_tokens:
                continue

            hit = len(q_tokens & c_tokens)
            if hit == 0:
                continue
            overlap = hit / len(q_tokens)

            ts = float(item.get("ts", 0) or 0)
            age_h = max(0.0, (now - ts) / 3600.0)
            decay = math.exp(-age_h * _LN2 / DECAY_HALFLIFE_H)

            freq = min(1.0, max(0, int(item.get("access_count", 0) or 0)) / 10.0)

            score = W_OVERLAP * overlap + W_DECAY * decay + W_FREQ * freq
            if score <= 0:
                continue

            shallow = dict(item)          # 浅拷贝：不动调用方的数据
            shallow["score"] = round(score, 6)
            scored.append(shallow)
    except Exception:
        # 任何意外都当作「没有记忆」，绝不让它冒泡到回复链路
        return []

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[: max(0, int(top_n))]
