"""``ParserWorker`` —— 工作线程：取请求 → 下载 → 校验 → 回调 → 分发结果。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, cast

from mineworker import setting
from mineworker.exceptions import (
    HttpStatusError,
    NotRetryError,
    RequestError,
    SpiderError,
    ValidationError,
)
from mineworker.network import circuit, robots, throttle
from mineworker.network import status as status_policy
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

        if response is None and request.auto_request and not robots.allowed(request.url):
            # 被 robots.txt 禁止是**有意跳过**，不是失败 —— 不该污染失败率，
            # 也不该进 failed_requests.jsonl 等着被回放
            self._stats.incr(sk.ROBOTS_DROPPED)
            log.debug("robots.txt 禁止，跳过：{}", request.url)
            self._stats.incr(sk.DROPPED)
            return

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

        # 状态码检查排在 validate 之前：状态码不对就不该再跑用户的校验与回调
        if response is not None and not self._check_status(request, response):
            return

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
        # 成功即清零该域的连续失败计数（熔断只认「连续」）
        circuit.record_success(request.url)

    def _check_status(self, request: Request, response: Response) -> bool:
        """按状态码策略放行 / 重试 / 判失败。返回 False 表示本次处理到此为止。"""
        verdict = status_policy.classify(response)
        if verdict == "ok":
            return True
        exc = HttpStatusError(response.status_code, response.url)
        reason = f"HTTP {response.status_code}"
        if verdict == "retry":
            # 被限速时把冷却抑制到整个域：只让撞上 429 的这个 worker 等是没用的，
            # 其余 worker 会继续满速打同一个域，退避形同虚设
            cooldown = status_policy.retry_after_seconds(response, now=time.time())
            if cooldown:
                # 惩罚要和 RETRY_AFTER_MAX 一起封顶：服务端说「一小时后再来」时我们
                # 已经决定放弃这个请求，就不该再给整个域挂一小时冷却 —— 那会让爬虫
                # 在该域上彻底停摆，且 worker 全睡在 throttle 里
                throttle.penalize(request.url, min(cooldown, setting.RETRY_AFTER_MAX))
            # 服务端要求等太久时不值得占着 worker 干等，直接判失败让位给别的任务
            too_long = status_policy.retry_after_too_long(response, now=time.time())
            if too_long is not None:
                self._fail(
                    request,
                    response,
                    reason=f"{reason}（Retry-After {too_long:.0f}s 超过上限）",
                    exc=exc,
                )
            else:
                self._retry_or_fail(request, response, exc, reason=reason)
        else:
            self._fail(request, response, reason=reason, exc=exc)
        return False

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
            self._fail(request, response, reason=reason, exc=exc)
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
        delay = status_policy.retry_delay(request, response, now=time.time())
        if delay > 0:
            time.sleep(delay)
        self._request_buffer.put_retry(request)

    def _fail(
        self,
        request: Request,
        response: Response | None,
        *,
        reason: str,
        exc: BaseException | None = None,
    ) -> None:
        self._stats.incr(sk.REQUEST_FAILED)
        # 在这里而不是每次失败都数：重试期间代理池已经轮换过出口，
        # 所以「代理坏了」会被重试吸收，只有站点真的挂了才会连续走到这里
        if circuit.counts_as_unhealthy(exc, response):
            circuit.record_failure(request.url)
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
