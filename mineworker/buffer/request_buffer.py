"""``RequestBuffer`` —— 收集 yield 出的 Request，去重后批量写入任务队列。"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from mineworker import setting
from mineworker.dedup import get_request_filter
from mineworker.utils import stats as stats_keys

if TYPE_CHECKING:
    from mineworker.core.task_queue import MemoryTaskQueue
    from mineworker.dedup import Filter
    from mineworker.network.request import Request
    from mineworker.utils.stats import Stats


class RequestBuffer(threading.Thread):
    def __init__(
        self,
        task_queue: MemoryTaskQueue,
        stats: Stats,
        *,
        dedup: Filter | None = None,
    ) -> None:
        super().__init__(name="request-buffer", daemon=True)
        self._queue = task_queue
        self._stats = stats
        self._dedup = dedup if dedup is not None else get_request_filter()
        self._pending: list[Request] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    def put(self, request: Request) -> None:
        with self._lock:
            self._pending.append(request)
        if len(self._pending) >= setting.REQUEST_BUFFER_MAX_CACHED:
            self.flush()

    def put_retry(self, request: Request) -> None:
        """重试请求：跳过去重再次入队。"""
        request.filter_repeat = False
        self.put(request)

    def is_empty(self) -> bool:
        with self._lock:
            return not self._pending

    # ------------------------------------------------------------------
    def flush(self) -> None:
        with self._lock:
            batch = self._pending
            self._pending = []
        for request in batch:
            if request.filter_repeat and not self._dedup.add(request.fingerprint):
                self._stats.incr(stats_keys.DEDUP_DROPPED)
                continue
            self._queue.put(request)

    def drain_pending(self) -> list[Request]:
        with self._lock:
            batch = self._pending
            self._pending = []
        return batch

    def run(self) -> None:
        while not self._stop_event.wait(setting.BUFFER_FLUSH_INTERVAL):
            self.flush()
        self.flush()

    def stop(self) -> None:
        self._stop_event.set()
