"""``LiteFilter`` —— 进程内精确去重（``set``）。

内存换准确：不会误判，但占用随去重量线性增长。海量场景请用阶段 03 的布隆过滤器。
"""

from __future__ import annotations

import threading


class LiteFilter:
    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def add(self, key: str) -> bool:
        """加入指纹。返回 ``True`` 表示新增（此前未见过），``False`` 表示重复。"""
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._seen

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)
