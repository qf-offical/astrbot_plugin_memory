"""最小 AstrBot 替身，让集成测试不需要装 AstrBot 也能跑。

`main.py` 顶层要 import 五个东西：

    from astrbot.api import logger
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.provider import ProviderRequest
    from astrbot.api.star import Star
    from astrbot.core.star.star_tools import StarTools

被测的只是 `_auto_learn` 这一条逻辑路径，它用不到任何真实的 AstrBot
运行时——真正被替换掉的只有 logger 和 Star 的构造。所以这里给一个
够用的桩，把 `astrbot.*` 装进 sys.modules，测试就能在任何机器上跑，
不必先把 AstrBot 装到 site-packages 里。

桩只在 `astrbot` 本来不可导入时才安装（真装了 AstrBot 的环境里
测的就是真货，不是替身）。
"""

from __future__ import annotations

import logging
import sys
import types


def _build() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    core = types.ModuleType("astrbot.core")
    star_pkg = types.ModuleType("astrbot.core.star")

    # ── logger ──────────────────────────────────────────────────────
    api.logger = logging.getLogger("astrbot_stub")

    # ── event ───────────────────────────────────────────────────────
    class AstrMessageEvent:  # noqa: D101 - 仅用于类型注解
        pass

    class _Filter:
        """收集装饰器：把原函数原样返回，不改变任何行为。"""

        @staticmethod
        def on_llm_request(*args, **kwargs):
            def deco(fn):
                fn._priority = kwargs.get("priority")
                return fn

            return deco

        @staticmethod
        def command(*args, **kwargs):
            def deco(fn):
                fn._command = args[0] if args else kwargs.get("name")
                return fn

            return deco

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = AstrMessageEvent
    event.filter = _Filter()

    # ── provider ────────────────────────────────────────────────────
    class ProviderRequest:  # noqa: D101
        pass

    provider = types.ModuleType("astrbot.api.provider")
    provider.ProviderRequest = ProviderRequest

    # ── star ────────────────────────────────────────────────────────
    class Star:  # noqa: D101
        def __init__(self, context=None) -> None:
            self.context = context

    star = types.ModuleType("astrbot.api.star")
    star.Star = Star

    # ── star_tools ──────────────────────────────────────────────────
    class StarTools:
        @staticmethod
        def get_data_dir(name: str):
            raise RuntimeError("stub: 测试不应真的去取数据目录")

    star_tools = types.ModuleType("astrbot.core.star.star_tools")
    star_tools.StarTools = StarTools

    # ── 组装 ────────────────────────────────────────────────────────
    astrbot.api = api
    astrbot.core = core
    api.event = event
    api.provider = provider
    api.star = star
    core.star = star_pkg
    star_pkg.star_tools = star_tools

    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.provider": provider,
        "astrbot.api.star": star,
        "astrbot.core": core,
        "astrbot.core.star": star_pkg,
        "astrbot.core.star.star_tools": star_tools,
    })


def install_if_missing() -> bool:
    """没有真 AstrBot 时装桩。返回 True 表示用的是替身。"""
    try:
        import astrbot  # noqa: F401
    except ImportError:
        _build()
        return True
    return False
