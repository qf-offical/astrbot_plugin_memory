import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.store import JsonlStore  # noqa: E402


def test_append_and_read(tmp_path):
    s = JsonlStore(tmp_path, "a.jsonl")
    s.append({"x": 1})
    s.append({"x": 2})
    assert [r["x"] for r in s.read_all()] == [1, 2]


def test_skip_corrupted_line(tmp_path):
    """写一半的 JSON（断电/崩溃）不能让整份记忆失效。"""
    f = tmp_path / "b.jsonl"
    f.write_text('{"x":1}\nNOT_JSON\n{"x":3}\n', encoding="utf-8")
    s = JsonlStore(tmp_path, "b.jsonl")
    assert [r["x"] for r in s.read_all()] == [1, 3]
    assert s.bad_lines == 1


def test_read_limit_returns_latest(tmp_path):
    s = JsonlStore(tmp_path, "c.jsonl")
    for i in range(10):
        s.append({"i": i})
    assert [r["i"] for r in s.read_all(limit=3)] == [7, 8, 9]


def test_rewrite_replaces_content(tmp_path):
    s = JsonlStore(tmp_path, "d.jsonl")
    s.append({"keep": False})
    s.append({"keep": True})
    s.rewrite([{"keep": True}])
    assert s.read_all() == [{"keep": True}]


def test_count(tmp_path):
    s = JsonlStore(tmp_path, "e.jsonl")
    assert s.count() == 0
    s.append({"a": 1})
    assert s.count() == 1


def test_missing_file_returns_empty(tmp_path):
    s = JsonlStore(tmp_path, "nope.jsonl")
    assert s.read_all() == []


def test_unicode_roundtrip(tmp_path):
    s = JsonlStore(tmp_path, "f.jsonl")
    s.append({"t": "中文测试 emoji 😀"})
    assert s.read_all()[0]["t"] == "中文测试 emoji 😀"


def test_creates_base_dir(tmp_path):
    nested = tmp_path / "a" / "b"
    s = JsonlStore(nested, "g.jsonl")
    s.append({"ok": True})
    assert (nested / "g.jsonl").exists()


def test_rewrite_is_atomic_no_tmp_left(tmp_path):
    s = JsonlStore(tmp_path, "h.jsonl")
    s.append({"a": 1})
    s.rewrite([{"a": 2}])
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_concurrent_append_no_loss(tmp_path):
    """4 线程各写 50 条，一条都不能丢。"""
    s = JsonlStore(tmp_path, "i.jsonl")

    def work():
        for i in range(50):
            s.append({"i": i})

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert s.count() == 200
    # 每行都必须是合法 JSON
    raw = (tmp_path / "i.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(raw) == 200
    for line in raw:
        json.loads(line)
