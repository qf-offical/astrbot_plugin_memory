import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.config import DEFAULTS, load_config  # noqa: E402


def test_defaults_when_none():
    c = load_config(None)
    assert c["working_size"] == 20
    assert c["recall_top_n"] == 3
    assert c["recall_max_chars"] == 800
    assert c["enabled"] is True


def test_override_keeps_others():
    c = load_config({"working_size": 5})
    assert c["working_size"] == 5
    assert c["recall_top_n"] == 3


def test_invalid_type_falls_back():
    c = load_config({"working_size": "abc"})
    assert c["working_size"] == DEFAULTS["working_size"]


def test_negative_clamped():
    c = load_config({"recall_top_n": -5})
    assert c["recall_top_n"] >= 1


def test_zero_clamped_to_min():
    c = load_config({"recall_max_chars": 0})
    assert c["recall_max_chars"] >= 50


def test_disabled_flag():
    assert load_config({"enabled": False})["enabled"] is False


def test_bool_as_int_accepted():
    """YAML 里 1/0 会被解析成 int，不该因此回落默认值。"""
    assert load_config({"working_size": 8})["working_size"] == 8


def test_unknown_keys_ignored():
    c = load_config({"nonsense": 123})
    assert "nonsense" not in c


def test_header_customizable():
    assert load_config({"inject_header": "【回忆】"})["inject_header"] == "【回忆】"
