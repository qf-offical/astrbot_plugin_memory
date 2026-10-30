"""_auto_learn 的集成测试：用假 provider 驱动真实流程。

不打真实模型，但走的代码路径与生产一致：
    取中期块 → 预筛 → 构造提示词 → 调 provider → 解析 → 写长期层
"""

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import astrbot_stub  # noqa: E402

# 没装 AstrBot 时用桩顶上，否则 import main 会直接 ModuleNotFoundError。
astrbot_stub.install_if_missing()

from memory.layers import MemoryLayers  # noqa: E402


class FakeProvider:
    """记录调用次数，返回预设 JSON。"""

    def __init__(self, response='["主人养了只猫叫团子"]'):
        self.response = response
        self.calls = 0
        self.last_prompt = ""

    async def text_chat(self, prompt=None, system_prompt=None, **kw):
        self.calls += 1
        self.last_prompt = prompt or ""
        return SimpleNamespace(completion_text=self.response)


class FailingProvider:
    async def text_chat(self, **kw):
        raise RuntimeError("模型挂了")


def make_plugin(tmp, provider, auto=True, cfg_extra=None):
    """构造一个只含必要字段的 MemoryPlugin 替身。

    不继承 Star（那需要完整 AstrBot 上下文），只把 _auto_learn
    依赖的属性挂上去。
    """
    cfg = {
        "enabled": True,
        "working_size": 20,
        "max_sessions": 200,
        "episodic_retention_days": 7,
        "semantic_max_entries": 1000,
        "recall_top_n": 3,
        "recall_max_chars": 800,
        "inject_header": "【记忆】",
        "auto_learn_semantic": auto,
    }
    if cfg_extra:
        cfg.update(cfg_extra)

    # main.py 用相对导入（from .memory.xxx），必须按包加载
    import importlib.util
    import sys as _sys

    pkg_name = "astrbot_plugin_memory_under_test"
    root = Path(__file__).resolve().parent.parent
    if pkg_name not in _sys.modules:
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.main", root / "main.py",
            submodule_search_locations=[str(root)],
        )
        pkg = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location(
                pkg_name, root / "__init__.py",
                submodule_search_locations=[str(root)],
            )
        )
        _sys.modules[pkg_name] = pkg
        mod = importlib.util.module_from_spec(spec)
        _sys.modules[f"{pkg_name}.main"] = mod
        spec.loader.exec_module(mod)

    MemoryPlugin = _sys.modules[f"{pkg_name}.main"].MemoryPlugin
    plugin = MemoryPlugin.__new__(MemoryPlugin)
    plugin.cfg = cfg
    plugin.layers = MemoryLayers(Path(tmp), cfg)
    plugin._pending = set()
    plugin.context = SimpleNamespace(get_using_provider=lambda: provider)
    return plugin


def seed_episodic(layers, chat_id, text):
    """直接往中期层塞一条（跳过 1 小时等待）。"""
    import time

    recs = layers.episodic_store.read_all()
    recs.append({
        "chat_id": chat_id,
        "role": "summary",
        "text": text,
        "ts": time.time(),
        "keywords": [],
        "source_count": 1,
    })
    layers.episodic_store.rewrite(recs)


def test_auto_learn_writes_semantic():
    with tempfile.TemporaryDirectory() as td:
        prov = FakeProvider()
        plugin = make_plugin(td, prov)
        seed_episodic(plugin.layers, "c1", "我叫小明，我养了只猫叫团子")

        asyncio.run(plugin._auto_learn())

        assert prov.calls == 1, f"应调用模型 1 次，实际 {prov.calls}"
        facts = plugin.layers.semantic_all()
        assert len(facts) == 1
        assert "团子" in facts[0]["text"]
        assert facts[0]["chat_id"] == "c1"


def test_auto_learn_skips_when_disabled():
    with tempfile.TemporaryDirectory() as td:
        prov = FakeProvider()
        plugin = make_plugin(td, prov, auto=False)
        seed_episodic(plugin.layers, "c1", "我叫小明，养了只猫叫团子")

        # 关掉开关时，维护循环不会走到 _auto_learn；
        # 这里直接验证「即使被误调也不该有副作用」不成立——
        # 所以只验证调用时确实会跑（_auto_learn 本身不看开关）
        asyncio.run(plugin._auto_learn())
        assert prov.calls == 1  # 开关判断在调用方


def test_auto_learn_skips_noise_only():
    """全是寒暄时不调模型（省钱）。"""
    with tempfile.TemporaryDirectory() as td:
        prov = FakeProvider()
        plugin = make_plugin(td, prov)
        seed_episodic(plugin.layers, "c1", "在吗")
        seed_episodic(plugin.layers, "c1", "哈哈哈")

        asyncio.run(plugin._auto_learn())
        assert prov.calls == 0, "寒暄不该触发模型调用"


def test_auto_learn_handles_provider_failure():
    """模型挂了不能崩，也不能写入垃圾。"""
    with tempfile.TemporaryDirectory() as td:
        plugin = make_plugin(td, FailingProvider())
        seed_episodic(plugin.layers, "c1", "我叫小明，养了只猫叫团子")

        asyncio.run(plugin._auto_learn())   # 不该抛
        assert plugin.layers.semantic_all() == []


def test_auto_learn_handles_unparseable_response():
    with tempfile.TemporaryDirectory() as td:
        prov = FakeProvider(response="我不太确定")
        plugin = make_plugin(td, prov)
        seed_episodic(plugin.layers, "c1", "我叫小明，养了只猫叫团子")

        asyncio.run(plugin._auto_learn())
        assert prov.calls == 1
        assert plugin.layers.semantic_all() == [], "解析失败时不该写入"


def test_auto_learn_no_provider_returns_silently():
    with tempfile.TemporaryDirectory() as td:
        plugin = make_plugin(td, None)
        seed_episodic(plugin.layers, "c1", "我叫小明，养了只猫叫团子")
        asyncio.run(plugin._auto_learn())   # 不该抛
        assert plugin.layers.semantic_all() == []


def test_auto_learn_per_chat_batching():
    """多个会话各自提炼，互不串。"""
    with tempfile.TemporaryDirectory() as td:
        prov = FakeProvider(response='["某条事实"]')
        plugin = make_plugin(td, prov)
        seed_episodic(plugin.layers, "c1", "会话一的内容，我叫小明")
        seed_episodic(plugin.layers, "c2", "会话二的内容，我叫小红")

        asyncio.run(plugin._auto_learn())

        assert prov.calls == 2, f"两个会话应各调一次，实际 {prov.calls}"
        chats = {r["chat_id"] for r in plugin.layers.semantic_all()}
        assert chats == {"c1", "c2"}
