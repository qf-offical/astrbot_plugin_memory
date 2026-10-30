"""pytest 公共设施。

主要解决一个问题：**怎么让一个文件「读不出来」，同时跨平台成立。**

POSIX 上 `os.chmod(path, 0o000)` 能触发 EACCES，Windows 上 chmod 只改
只读位，文件照样读得出来——测试于是在 Windows 上「假失败」。

这里改成拦截内建 `open()` 抛 PermissionError。被测代码（memory.store）
走的就是内建 open，仍然是同一条 `except OSError` 分支，但行为在三个
平台上完全一致，也不依赖文件的真实权限位。

注意：只拦内建 open，不动 `io.open`。所以 pathlib 的 `read_text`/
`write_text` 不受影响——测试自己准备夹具、事后回读校验时不会被误伤。
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest


class _ReadFailure:
    """可反复开关的读失败开关。

    `fail_read(path)`       只让这一个文件读不了
    `fail_read.restore()`   关掉，恢复正常读取（同一用例里还想回读校验时用）
    """

    def __init__(self) -> None:
        self._target: Path | None = None

    def __call__(self, target) -> "_ReadFailure":
        self._target = Path(target).resolve()
        return self

    def restore(self) -> None:
        self._target = None

    def should_fail(self, file, mode: str) -> bool:
        if self._target is None:
            return False
        # 只拦「读」：写路径必须保持可用，否则测不出「读失败后有没有误写」。
        if any(c in mode for c in "wax+"):
            return False
        try:
            return Path(file).resolve() == self._target
        except (TypeError, OSError, ValueError):
            return False  # 传进来的是 fd 之类的非路径对象


@pytest.fixture
def fail_read(monkeypatch):
    """`fail_read(path)` 之后，读该文件会抛 PermissionError。"""
    ctl = _ReadFailure()
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if ctl.should_fail(file, mode):
            raise PermissionError(13, "Permission denied（测试模拟的读失败）", str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    return ctl
