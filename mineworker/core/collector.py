"""``MemoryCollector`` —— 在任务队列和工作线程之间做一层批量缓冲。

对内存队列而言几乎是透传；保留它是为了给 Roadmap v2 的 Redis 队列留批量拉取的位置，
同时也是结束检测判断「是否还有在途任务」的一个观测点。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING

from mineworker import setting

if TYPE_CHECKING:
    from mineworker.core.task_queue import MemoryTaskQueue
    from mineworker.network.request import Request


class MemoryCollector:
    def __init__(self, task_queue: MemoryTaskQueue) -> None:
        self._queue = task_queue
        self._buffer: deque[Request] = deque()
        self._lock = threading.Lock()

    def get_request(self, timeout: float = 1.0) -> Request | None:
        with self._lock:
            if self._buffer:
                return self._buffer.popleft()
        # 缓冲空：从队列阻塞取一个，再顺手多取几个填充缓冲
        first = self._queue.get(timeout=timeout)
        if first is None:
            return None
        extra: list[Request] = []
        for _ in range(setting.COLLECTOR_TASK_COUNT - 1):
            nxt = self._queue.get(timeout=0)
            if nxt is None:
                break
            extra.append(nxt)
        if extra:
            with self._lock:
                self._buffer.extend(extra)
        return first

    def is_empty(self) -> bool:
        with self._lock:
            buffered = bool(self._buffer)
        return not buffered and self._queue.empty()

    def drain(self) -> list[Request]:
        with self._lock:
            items = list(self._buffer)
            self._buffer.clear()
        return items
