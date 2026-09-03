"""内存任务队列。

按 ``request.priority`` 出队（值小者先出）；同优先级按入队顺序。阶段 02 的
单进程实现；Roadmap v2 会换成 Redis zset，接口保持一致。
"""

from __future__ import annotations

import itertools
import queue
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mineworker.network.request import Request


class MemoryTaskQueue:
    def __init__(self) -> None:
        self._q: queue.PriorityQueue[tuple[int, int, Request]] = queue.PriorityQueue()
        self._seq = itertools.count()

    def put(self, request: Request) -> None:
        self._q.put((request.priority, next(self._seq), request))

    def get(self, timeout: float | None = None) -> Request | None:
        try:
            return self._q.get(timeout=timeout)[2]
        except queue.Empty:
            return None

    def qsize(self) -> int:
        return self._q.qsize()

    def empty(self) -> bool:
        return self._q.empty()
