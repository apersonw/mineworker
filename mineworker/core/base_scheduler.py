"""``BaseScheduler`` —— 调度器公共骨架。

AirScheduler / RedisScheduler 复用这里的线程编排、结束检测循环、优雅退出、
指标 / 告警接入；子类只需实现「用哪种队列 / 去重」「怎么注入种子」「什么算结束」。
"""

from __future__ import annotations

import contextlib
import signal
import threading
import time
from types import FrameType
from typing import TYPE_CHECKING, Any

from mineworker import setting
from mineworker.buffer.item_buffer import ItemBuffer, ItemHandler
from mineworker.buffer.request_buffer import RequestBuffer
from mineworker.core.collector import Collector
from mineworker.core.parser_control import ParserWorker
from mineworker.exceptions import SpiderError
from mineworker.network.downloader import close_default_downloaders
from mineworker.network.middleware import MiddlewareManager
from mineworker.network.proxy_pool import close_proxy_pool
from mineworker.network.request import Request
from mineworker.utils.alert import AlertManager
from mineworker.utils.log import get_logger
from mineworker.utils.metrics import MetricsReporter
from mineworker.utils.stats import Stats

if TYPE_CHECKING:
    from mineworker.core.base_parser import BaseParser
    from mineworker.dedup import Filter

log = get_logger("scheduler")

_JOIN_TIMEOUT = 8.0


class BaseScheduler:
    def __init__(
        self,
        parser: BaseParser,
        *,
        thread_count: int | None = None,
        item_handler: ItemHandler | None = None,
        pipelines: list[str] | None = None,
    ) -> None:
        self._parser = parser
        self._thread_count = 1 if setting.DEBUG else (thread_count or setting.SPIDER_THREAD_COUNT)
        self.stats = Stats()
        self._task_queue = self._make_task_queue()
        self._request_buffer = RequestBuffer(self._task_queue, self.stats, dedup=self._make_dedup())
        self._item_buffer = ItemBuffer(self.stats, handler=item_handler, pipelines=pipelines)
        self._collector = Collector(self._task_queue)
        self._middleware = MiddlewareManager(setting.DOWNLOADER_MIDDLEWARES)
        self._user_pool = parser.user_pool()
        if self._user_pool is not None:
            from mineworker.network.user_pool.middleware import UserPoolMiddleware

            self._middleware.prepend(
                UserPoolMiddleware(self._user_pool, check_login=parser.check_login)
            )
            log.info("账号池已挂载：{}", type(self._user_pool).__name__)
        self._alert = AlertManager(self.stats)
        self._metrics: MetricsReporter | None = None
        self._workers: list[ParserWorker] = []
        self._stop_event = threading.Event()
        self._interrupted = False
        #: 安装前的原始信号处理器，退出时还原
        self._orig_handlers: dict[signal.Signals, Any] = {}

    # ------------------------------------------------------------------
    # 子类钩子
    # ------------------------------------------------------------------
    def _make_task_queue(self) -> Any:
        raise NotImplementedError

    def _make_dedup(self) -> Filter | None:
        return None  # None => RequestBuffer 用 setting.DEDUP_FILTER 的默认实现

    def _seed(self) -> None:
        count = self._seed_requests()
        if count:
            log.info("种子请求 {} 条", count)
        else:
            log.warning("start_requests 没有产出任何请求")

    def _is_done(self) -> bool:
        raise NotImplementedError

    def _on_start(self) -> None:
        """线程都起来、种子未注入前调用。"""

    def _on_shutdown(self) -> None:
        """teardown 末尾调用。"""

    def _on_failed_request(self, request: Request) -> None:
        """某请求重试耗尽后调用（在 failed_request 钩子之后）。默认无操作。"""

    # ------------------------------------------------------------------
    def run(self) -> None:
        log.info("爬虫启动（{} 个工作线程）", self._thread_count)
        self._parser.start_callback()
        self._install_signal()
        self._seed()  # 必须在工作线程启动前：否则 worker 会抢跑掉断点续爬的队列
        self._start_threads()
        self._on_start()
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
    def _seed_requests(self) -> int:
        count = 0
        for request in self._parser.start_requests() or ():
            if not isinstance(request, Request):
                raise SpiderError(f"start_requests 只能 yield Request，收到 {type(request)!r}")
            self._request_buffer.put(request)
            count += 1
        self._request_buffer.flush()
        return count

    def _local_pending(self) -> int:
        return (
            self._request_buffer.pending_count()
            + self._collector.buffered_count()
            + self._item_buffer.pending_count()
            + sum(worker.busy for worker in self._workers)
        )

    def _local_idle(self) -> bool:
        return (
            self._request_buffer.is_empty()
            and self._collector.is_empty()
            and self._item_buffer.is_empty()
            and all(not worker.busy for worker in self._workers)
        )

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
                middleware=self._middleware,
                failed_sink=self._on_failed_request,
            )
            worker.start()
            self._workers.append(worker)
        if setting.METRICS_ENABLE:
            self._metrics = MetricsReporter(
                self.stats,
                {
                    "queue_depth": self._task_queue.qsize,
                    "in_flight": lambda: sum(w.busy for w in self._workers),
                },
            )
            self._metrics.start()

    def _wait_until_done(self) -> None:
        streak = 0
        limit = setting.SPIDER_MAX_RUNTIME
        deadline = time.monotonic() + limit if limit > 0 else None
        while not self._stop_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                # 不设 _interrupted：那个标志只服务于「再按一次 Ctrl-C 强制退出」，
                # 超时不该改变后续 SIGINT 的语义。这里走正常的 teardown 路径：
                # flush 缓冲区、dump 未完成请求，然后正常返回。
                log.warning("达到 SPIDER_MAX_RUNTIME={:.0f}s，优雅停止", limit)
                self._stop_event.set()
                return
            if self._is_done():
                streak += 1
                if streak >= setting.DONE_CHECK_TIMES:
                    return
            else:
                streak = 0
            self._alert.check()
            self._stop_event.wait(setting.DONE_CHECK_INTERVAL)

    def _teardown(self) -> None:
        for worker in self._workers:
            worker.stop()
        for worker in self._workers:
            worker.join(timeout=_JOIN_TIMEOUT)
        if self._metrics is not None:
            self._metrics.stop()
            self._metrics.join(timeout=_JOIN_TIMEOUT)
        self._request_buffer.stop()
        self._item_buffer.stop()
        self._request_buffer.join(timeout=_JOIN_TIMEOUT)
        self._item_buffer.join(timeout=_JOIN_TIMEOUT)
        self._request_buffer.flush()
        self._item_buffer.flush()
        self._item_buffer.close()
        self._restore_signal()
        close_default_downloaders()
        close_proxy_pool()
        if self._user_pool is not None:
            self._user_pool.close()
        self._on_shutdown()

    # ------------------------------------------------------------------
    #: 都要优雅接管。**SIGTERM 尤其重要**：docker stop / K8s 驱逐 / systemctl stop
    #: 全都发它，而分布式节点被硬杀时，collector 本地缓冲里最多 COLLECTOR_TASK_COUNT
    #: 个已从 Redis 领走（zpopmin 取走即删）的任务会永久丢失 —— 实测 24 个任务的
    #: 场景下 SIGTERM 丢了 20 个，SIGINT 则全部恢复。
    _SIGNALS = (signal.SIGINT, signal.SIGTERM)

    def _install_signal(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in self._SIGNALS:
            try:
                self._orig_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):  # pragma: no cover - 平台不支持该信号
                self._orig_handlers.pop(sig, None)

    def _restore_signal(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig, handler in list(self._orig_handlers.items()):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)
        self._orig_handlers.clear()

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        name = signal.Signals(signum).name
        if self._interrupted:
            log.warning("再次收到 {}，强制退出", name)
            self._restore_signal()
            raise KeyboardInterrupt
        self._interrupted = True
        log.warning("收到 {}，正在优雅停止（再来一次强制退出）", name)
        self._stop_event.set()
