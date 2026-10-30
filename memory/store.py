"""JSONL 存储：一行一条记录，坏行跳过，重写原子替换。

为什么用 JSONL 而不是 sqlite3：
    · 记忆量很小（每天 < 100KB），不值得引入数据库
    · 出问题能直接 `cat` 查看、手工修一行
    · 不需要处理 schema 迁移

并发：
    每个实例持一把 threading.Lock。追加是「打开 → 写一行 → 关闭」，
    锁保证不会有两个线程同时写同一文件造成行撕裂。跨进程并发不在
    支持范围内（AstrBot 插件单进程运行）。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

# 单文件超过这个大小，layer 层会调用 rewrite 做轮转（这里只提供能力）
ROTATE_BYTES = 5 * 1024 * 1024


class StoreReadError(RuntimeError):
    """读存储失败。

    与「文件不存在」（正常返回空）严格区分：读失败说明底层有问题
    （权限、挂载、IO），此时任何基于「读到空 = 没有数据」的写入都会
    摧毁已有记忆，必须让调用方中止。
    """


class JsonlStore:
    """一个 JSONL 文件的读写封装。"""

    def __init__(self, base_dir: Path, filename: str) -> None:
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / filename
        self.bad_lines = 0
        self._lock = threading.Lock()

    # ── 写 ──────────────────────────────────────────────────────────────
    def append(self, record: dict[str, Any]) -> None:
        """追加一条记录。父目录不存在时自动创建。"""
        with self._lock:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def rewrite(self, records: list[dict[str, Any]]) -> None:
        """整体替换。

        先写 .tmp 再 os.replace：中途崩了原文件仍然完整，不会出现
        「写到一半的半个文件」。这也是 rename 在 POSIX 上的原子语义。
        """
        with self._lock:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    for r in records:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                os.replace(tmp, self.path)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

    # ── 读 ──────────────────────────────────────────────────────────────
    def read_all(self, limit: int | None = None) -> list[dict[str, Any]]:
        """读全部；limit 时取**最后** N 条（越新越靠后）。

        坏行计入 self.bad_lines 后跳过 —— 一条坏行不该让整份记忆失效。
        """
        if not self.path.exists():
            return []

        records: list[dict[str, Any]] = []
        self.bad_lines = 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        self.bad_lines += 1
                        continue
                    if isinstance(obj, dict):
                        records.append(obj)
        except OSError as e:
            # ⚠️ 绝对不能在这里 return []。
            #
            # 调用方（layers.record）是「read_all → 修改 → rewrite」的事务。
            # 读失败时返回空列表，等于告诉它「当前没有记忆」，于是它拿空列表
            # 当基准重写整个文件 —— 原有的记忆被一次性抹掉。
            # 实测：9 条记忆 chmod 000 后写 1 条，只剩 1 条，8 条永久丢失。
            #
            # EACCES / 只读挂载 / NFS 抖动都会触发。抛出去让调用方中止写入，
            # 宁可这一条记不上，也不能把历史清空。
            raise StoreReadError(f"读取 {self.path.name} 失败：{e}") from e

        if limit is not None and limit >= 0:
            return records[-limit:] if limit else []
        return records

    def count(self) -> int:
        return len(self.read_all())

    # ── 维护 ────────────────────────────────────────────────────────────
    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def needs_rotate(self) -> bool:
        return self.size_bytes() > ROTATE_BYTES
