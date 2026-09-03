"""``AirScheduler`` —— 单进程调度器：编排缓冲区 / 采集器 / 工作线程，并检测结束。"""

from __future__ import annotations

import signal
import threading
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any

from mineworker import setting
from mineworker.buffer.item_buffer import ItemBuffer, ItemHandler
from mineworker.buffer.request_buffer import RequestBuffer
from mineworker.core.collector import MemoryCollector
from mineworker.core.parser_control import ParserWorker
from mineworker.core.task_queue import MemoryTaskQueue
from mineworker.exceptions import SpiderError
from mineworker.network.downloader import close_default_downloaders
from mineworker.network.request import Request
from mineworker.utils import tools
from mineworker.utils.log import get_logger
from mineworker.utils.stats import Stats

if TYPE_CHECKING:
    from mineworker.core.base_parser import BaseParser

log = get_logger("scheduler")

_JOIN_TIMEOUT = 8.0


class AirScheduler:
    def __init__(
        self,
        parser: BaseParser,
        *,
        thread_count: int | None = None,
        item_handler: ItemHandler | None = None,
    ) -> None:
        self._parser = parser
        self._thread_count = thread_count or setting.SPIDER_THREAD_COUNT
        self.stats = Stats()
        self._task_queue = MemoryTaskQueue()
        self._request_buffer = RequestBuffer(self._task_queue, self.stats)
        self._item_buffer = ItemBuffer(self.stats, handler=item_handler)
        self._collector = MemoryCollector(self._task_queue)
        self._workers: list[ParserWorker] = []
        self._stop_event = threading.Event()
        self._interrupted = False
        self._orig_sigint: Any = None  # signal._HANDLER，类型太琐碎

    # ------------------------------------------------------------------
    def run(self) -> None:
        log.info("爬虫启动（{} 个工作线程）", self._thread_count)
        self._parser.start_callback()
        self._install_signal()
        self._start_threads()
        self._seed()
        try:
            self._wait_until_done()
        finally:
            self._teardown()
        self._parser.end_callback()
        log.info("爬虫结束 | {}", self.stats.summary())

    def stop(self) -> None:
        self._interrupted = True
        self._stop_event.set()

    # ------------------------------------------------------------------
    def _start_threads(self) -> None:
        self._request_buffer.start()
        self._item_buffer.start()
        for i in range(self._thread_count):
            worker = ParserWorker(
                i,
                parser=self._parser,
                collector=self._collector,
                request_buffer=self._request_buffer,
                item_buffer=self._item_buffer,
                stats=self.stats,
            )
            worker.start()
            self._workers.append(worker)

    def _seed(self) -> None:
        count = 0
        for request in self._parser.start_requests() or ():
            if not isinstance(request, Request):
                raise SpiderError(f"start_requests 只能 yield Request，收到 {type(request)!r}")
            self._request_buffer.put(request)
            count += 1
        self._request_buffer.flush()
        if count:
            log.info("种子请求 {} 条", count)
        else:
            log.warning("start_requests 没有产出任何请求")

    def _all_idle(self) -> bool:
        return (
            self._request_buffer.is_empty()
            and self._task_queue.empty()
            and self._collector.is_empty()
            and self._item_buffer.is_empty()
            and all(not worker.busy for worker in self._workers)
        )

    def _wait_until_done(self) -> None:
        idle_streak = 0
        while not self._stop_event.is_set():
            if self._all_idle():
                idle_streak += 1
                if idle_streak >= setting.DONE_CHECK_TIMES:
                    return
            else:
                idle_streak = 0
            self._stop_event.wait(setting.DONE_CHECK_INTERVAL)

    def _teardown(self) -> None:
        for worker in self._workers:
            worker.stop()
        for worker in self._workers:
            worker.join(timeout=_JOIN_TIMEOUT)
        self._request_buffer.stop()
        self._item_buffer.stop()
        self._request_buffer.join(timeout=_JOIN_TIMEOUT)
        self._item_buffer.join(timeout=_JOIN_TIMEOUT)
        self._request_buffer.flush()
        self._item_buffer.flush()
        self._restore_signal()
        close_default_downloaders()
        if self._interrupted and setting.DUMP_UNFINISHED_ON_EXIT:
            self._dump_unfinished()

    def _dump_unfinished(self) -> None:
        leftovers: list[Request] = [
            *self._request_buffer.drain_pending(),
            *self._collector.drain(),
        ]
        while (leftover := self._task_queue.get(timeout=0)) is not None:
            leftovers.append(leftover)
        if not leftovers:
            return
        path = Path(setting.FAILED_REQUEST_PATH)
        with path.open("a", encoding="utf-8") as fh:
            for request in leftovers:
                fh.write(tools.dumps_json(request.to_dict()) + "\n")
        log.warning("已 dump {} 条未完成请求到 {}", len(leftovers), path)

    # ------------------------------------------------------------------
    def _install_signal(self) -> None:
        if threading.current_thread() is threading.main_thread():
            self._orig_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._on_sigint)

    def _restore_signal(self) -> None:
        if self._orig_sigint is not None and threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._orig_sigint)
            self._orig_sigint = None

    def _on_sigint(self, signum: int, frame: FrameType | None) -> None:
        if self._interrupted:
            log.warning("再次收到中断，强制退出")
            self._restore_signal()
            raise KeyboardInterrupt
        self._interrupted = True
        log.warning("收到中断，正在优雅停止（再按一次强制退出）")
        self._stop_event.set()
