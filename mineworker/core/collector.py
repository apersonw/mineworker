"""``Collector`` —— 在任务队列和工作线程之间做一层批量缓冲。

对内存队列几乎是透传；对 Redis 队列则用 ``get_batch`` 一次多取，减少往返。
也是结束检测判断「本节点是否还有在途任务」的观测点。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING, Any

from mineworker import setting

if TYPE_CHECKING:
    from mineworker.network.request import Request


class Collector:
    def __init__(self, task_queue: Any) -> None:
        self._queue = task_queue
        self._buffer: deque[Request] = deque()
        self._lock = threading.Lock()

    def get_request(self, timeout: float = 1.0) -> Request | None:
        with self._lock:
            if self._buffer:
                return self._buffer.popleft()
        first: Request | None = self._queue.get(timeout=timeout)
        if first is None:
            return None
        extra: list[Request] = self._queue.get_batch(setting.COLLECTOR_TASK_COUNT - 1)
        if extra:
            with self._lock:
                self._buffer.extend(extra)
        return first

    def is_empty(self) -> bool:
        with self._lock:
            buffered = bool(self._buffer)
        return not buffered and self._queue.empty()

    def buffered_count(self) -> int:
        with self._lock:
            return len(self._buffer)

    def drain(self) -> list[Request]:
        with self._lock:
            items = list(self._buffer)
            self._buffer.clear()
        return items


#: 兼容旧名
MemoryCollector = Collector
