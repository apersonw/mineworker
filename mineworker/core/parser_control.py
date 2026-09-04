"""``ParserWorker`` —— 工作线程：取请求 → 下载 → 校验 → 回调 → 分发结果。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, cast

from mineworker import setting
from mineworker.exceptions import NotRetryError, RequestError, SpiderError, ValidationError
from mineworker.network.downloader import download_request
from mineworker.network.middleware import MiddlewareManager
from mineworker.network.request import Request
from mineworker.network.response import Response
from mineworker.utils import stats as sk
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.buffer.item_buffer import ItemBuffer
    from mineworker.buffer.request_buffer import RequestBuffer
    from mineworker.core.base_parser import BaseParser
    from mineworker.core.collector import Collector
    from mineworker.utils.stats import Stats

_FailedSink = Callable[[Request], None]

log = get_logger("worker")

_Callback = Callable[..., "Iterable[Any] | None"]


class ParserWorker(threading.Thread):
    def __init__(
        self,
        index: int,
        *,
        parser: BaseParser,
        collector: Collector,
        request_buffer: RequestBuffer,
        item_buffer: ItemBuffer,
        stats: Stats,
        middleware: MiddlewareManager | None = None,
        failed_sink: _FailedSink | None = None,
    ) -> None:
        super().__init__(name=f"worker-{index}", daemon=True)
        self._parser = parser
        self._collector = collector
        self._request_buffer = request_buffer
        self._item_buffer = item_buffer
        self._stats = stats
        self._middleware = middleware or MiddlewareManager()
        self._failed_sink = failed_sink
        self._stop_event = threading.Event()
        self.busy = False

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    def run(self) -> None:
        while not self._stop_event.is_set():
            request = self._collector.get_request(timeout=0.3)
            if request is None:
                self.busy = False
                continue
            self.busy = True
            try:
                self._process(request)
            except Exception:
                log.exception("worker 未捕获异常")
            finally:
                self.busy = False

    # ------------------------------------------------------------------
    def _process(self, request: Request) -> None:
        response: Response | None = None
        try:
            mw_out = self._middleware.process_request(request)
            if isinstance(mw_out, Response):
                response = mw_out
            else:
                request = mw_out
            replaced = self._parser.download_midware(request)
        except Exception as exc:
            self._retry_or_fail(request, None, exc, reason="download_midware 异常")
            return
        if replaced is not None:
            request = replaced

        if response is None and request.auto_request:
            try:
                response = download_request(request)
            except RequestError as exc:
                self._retry_or_fail(request, None, exc, reason="下载失败")
                return

        if response is not None:
            resp_out = self._middleware.process_response(request, response)
            if isinstance(resp_out, Request):
                resp_out.filter_repeat = False
                self._request_buffer.put(resp_out)
                self._stats.incr(sk.REQUEST_OK)
                return
            response = resp_out

        if response is not None and not self._validate(request, response):
            return

        callback = self._resolve_callback(request)
        try:
            # 生成器回调的异常会在迭代时才抛出，因此调用与分发放在同一 try 内
            self._dispatch(callback(request, response, **request.cb_kwargs))
        except NotRetryError:
            self._drop(request)
            return
        except Exception as exc:
            self._stats.incr(sk.PARSE_ERROR)
            self._retry_or_fail(request, response, exc, reason="解析异常")
            return

        self._stats.incr(sk.REQUEST_OK)

    def _validate(self, request: Request, response: Response) -> bool:
        try:
            ok = self._parser.validate(request, response)
        except ValidationError as exc:
            self._retry_or_fail(request, response, exc, reason="校验失败")
            return False
        except NotRetryError:
            self._drop(request)
            return False
        if ok is False:
            self._drop(request)
            return False
        return True

    def _resolve_callback(self, request: Request) -> _Callback:
        cb = request.callback
        if cb is None:
            return self._parser.parse
        if callable(cb):
            return cb
        method = getattr(self._parser, cb, None)
        if not callable(method):
            raise SpiderError(f"找不到回调方法 {cb!r}（parser={type(self._parser).__name__}）")
        return cast("_Callback", method)

    def _dispatch(self, results: Iterable[Any] | None) -> None:
        if results is None:
            return
        for obj in results:
            if isinstance(obj, Request):
                self._request_buffer.put(obj)
            elif callable(obj):
                obj()
            else:
                self._item_buffer.put(obj)

    # ------------------------------------------------------------------
    def _retry_or_fail(
        self,
        request: Request,
        response: Response | None,
        exc: BaseException,
        *,
        reason: str,
    ) -> None:
        if request.retry_times >= setting.SPIDER_MAX_RETRY_TIMES:
            self._fail(request, response, reason=reason)
            return
        request.retry_times += 1
        self._stats.incr(sk.RETRY)
        try:
            self._parser.exception_request(request, response, exc)
        except Exception:
            log.exception("exception_request 钩子异常")
        log.warning(
            "{}，第 {}/{} 次重试：{} {}",
            reason,
            request.retry_times,
            setting.SPIDER_MAX_RETRY_TIMES,
            request.method,
            request.url,
        )
        if setting.SPIDER_RETRY_INTERVAL:
            time.sleep(setting.SPIDER_RETRY_INTERVAL)
        self._request_buffer.put_retry(request)

    def _fail(self, request: Request, response: Response | None, *, reason: str) -> None:
        self._stats.incr(sk.REQUEST_FAILED)
        log.error(
            "{}，放弃：{} {}（已重试 {} 次）",
            reason,
            request.method,
            request.url,
            request.retry_times,
        )
        try:
            self._dispatch(self._parser.failed_request(request, response))
        except Exception:
            log.exception("failed_request 钩子异常")
        if self._failed_sink is not None:
            try:
                self._failed_sink(request)
            except Exception:
                log.exception("failed_sink 异常")

    def _drop(self, request: Request) -> None:
        self._stats.incr(sk.DROPPED)
        log.debug("丢弃：{} {}", request.method, request.url)
