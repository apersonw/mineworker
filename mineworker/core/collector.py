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
        #: 已交给 worker、还没处理完的任务。续租约要用 ——
        #: 光续缓冲区里的不够：正在被处理的那些恰恰是耗时最长、最容易超时的
        self._in_progress: dict[int, Request] = {}
        self._lock = threading.Lock()

    def get_request(self, timeout: float = 1.0) -> Request | None:
        with self._lock:
            if self._buffer:
                request = self._buffer.popleft()
                self._in_progress[id(request)] = request
                return request
        first: Request | None = self._queue.get(timeout=timeout)
        if first is None:
            return None
        extra: list[Request] = self._queue.get_batch(setting.COLLECTOR_TASK_COUNT - 1)
        if extra:
            with self._lock:
                self._buffer.extend(extra)
        self._mark_in_progress(first)
        return first

    def done(self, request: Request) -> None:
        """任务处理完了，给队列销账（内存队列没有租约，静默跳过）。

        不销账的话，任务会一直挂在在途表里，直到租约到期被当成「节点死了」
        重新放回队列 —— 于是每个任务都被抓两遍。
        """
        with self._lock:
            self._in_progress.pop(id(request), None)
        done = getattr(self._queue, "done", None)
        if done is not None:
            done(request)

    def _mark_in_progress(self, request: Request) -> None:
        with self._lock:
            self._in_progress[id(request)] = request

    def held_requests(self) -> list[Request]:
        """本节点当前持有的全部任务：缓冲区里排队的 + worker 正在处理的。

        两部分都要续租约。只续缓冲区的话，正在处理的那些反而最容易超时 ——
        它们恰恰是耗时最长的那批。
        """
        with self._lock:
            return [*self._buffer, *self._in_progress.values()]

    def is_empty(self) -> bool:
        with self._lock:
            busy = bool(self._buffer) or bool(self._in_progress)
        return not busy and self._queue.empty()

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
