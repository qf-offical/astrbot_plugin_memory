"""插件配置：默认值 + 类型校验 + 范围钳制。

为什么每一项都要校验而不是直接 config.get(k, default)：
    AstrBot 的配置来自 WebUI 表单和 cmd_config.json，用户手改一个错值
    （比如 working_size 写成 "20" 字符串、recall_top_n 写成 -1）不该让
    插件崩掉或行为诡异。这里统一兜住，非法值静默回落默认。
"""

from __future__ import annotations

from typing import Any

# 每项：默认值 + 合法范围（数值型）+ 类型
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "working_size": 20,           # 短期每会话保留条数 1~200
    "max_sessions": 200,          # 短期最多保留多少个会话 10~5000
    "episodic_retention_days": 7,  # 中期保留天数 1~365
    "semantic_max_entries": 1000,  # 长期条数上限 10~100000
    "recall_top_n": 3,             # 每次注入几条 1~10
    "recall_max_chars": 800,       # 注入块最大字符 50~4000
    "inject_header": "【记忆】",
    "auto_learn_semantic": False,  # 后台用模型提炼长期记忆
    "maintenance_interval_min": 10,  # 后台整理间隔（分钟）
}

_RANGES: dict[str, tuple[int, int]] = {
    "working_size": (1, 200),
    "max_sessions": (10, 5000),
    "episodic_retention_days": (1, 365),
    "semantic_max_entries": (10, 100_000),
    "recall_top_n": (1, 10),
    "recall_max_chars": (50, 4000),
    "maintenance_interval_min": (1, 1440),
}


def _as_int(value: Any, default: int) -> int:
    """尽力转成 int；失败返回默认值。bool 单独处理（True 不是 1）。"""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """把外部配置合并到默认值上，逐项校验。"""
    cfg = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return cfg

    for key, default in DEFAULTS.items():
        if key not in raw:
            continue
        value = raw[key]

        if isinstance(default, bool):
            # 只认真正的 bool；字符串 "false" 之类容易被误解，保守回落
            if isinstance(value, bool):
                cfg[key] = value
            continue

        if isinstance(default, int):
            n = _as_int(value, default)
            lo, hi = _RANGES.get(key, (None, None))
            if lo is not None:
                n = max(lo, min(hi, n))
            cfg[key] = n
            continue

        if isinstance(default, str):
            if isinstance(value, str) and value.strip():
                cfg[key] = value.strip()
            continue

    return cfg
