"""中间件发起的重试要计入重试预算。

`process_response` 返回一个 Request 就是「这个响应不算数，换一个请求重来」——
账号池的「掉登录，换号重试」走的正是这条路。

但这条路原本**既不递增 `retry_times` 也不走 `_retry_or_fail`**，
而中间件还会清掉 `filter_repeat`：重试上限管不住它、去重也拦不住它。
实测（1 个页面、SPIDER_MAX_RETRY_TIMES=3、账号池空 + 允许匿名、站点一直回登录墙）：
**目标站被打了 76 次 / 8 秒**，只被 SPIDER_MAX_RUNTIME 拦住。

机制本身一直没有预算，但 0.13.0 之前它有个**偶然的**终止条件：每次重试都会拉黑
一个账号，池子迟早空掉，那时旧代码直接放行响应。0.13.0 让「没挂账号也检查」，
把那个终止条件拿掉了 —— 循环再也停不下来。
"""

from __future__ import annotations

from typing import Any

import pytest

from mineworker import setting
from mineworker.core.parser_control import ParserWorker
from mineworker.network.request import Request
from mineworker.network.response import Response
from mineworker.utils import stats as sk
from mineworker.utils.stats import Stats


class _Buffer:
    def __init__(self) -> None:
        self.put_back: list[Request] = []

    def put(self, request: Request) -> None:
        self.put_back.append(request)


class _Sink:
    def __init__(self) -> None:
        self.failed: list[Request] = []

    def __call__(self, request: Request) -> None:
        self.failed.append(request)


def _worker(buffer: _Buffer, stats: Stats, sink: _Sink) -> Any:
    from mineworker.core.base_parser import BaseParser

    worker = ParserWorker.__new__(ParserWorker)
    worker._parser = BaseParser()
    worker._request_buffer = buffer
    worker._stats = stats
    worker._failed_sink = sink
    worker._item_buffer = None
    worker._deferred = False
    return worker


def _resp(request: Request) -> Response:
    return Response(url=request.url, status_code=200, content=b"<h1>x</h1>", request=request)


@pytest.fixture(autouse=True)
def _limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "SPIDER_MAX_RETRY_TIMES", 3)


def test_replacement_retry_consumes_the_budget() -> None:
    """换过的那个请求要从原请求接过预算 —— 从 0 开始等于没加上限。"""
    buffer, stats, sink = _Buffer(), Stats(), _Sink()
    worker = _worker(buffer, stats, sink)
    original = Request("http://example.com/p/1")
    original.retry_times = 2
    worker._retry_replacement(original, original.copy(), _resp(original))

    assert len(buffer.put_back) == 1
    assert buffer.put_back[0].retry_times == 3, "预算没接过来，重试会无限继续"


def test_replacement_retry_stops_at_the_limit() -> None:
    """到上限就判失败落盘，而不是再入队一次。"""
    buffer, stats, sink = _Buffer(), Stats(), _Sink()
    worker = _worker(buffer, stats, sink)
    original = Request("http://example.com/p/1")
    original.retry_times = 3  # 已经用满
    worker._retry_replacement(original, original.copy(), _resp(original))

    assert buffer.put_back == [], "超过重试上限还在重新入队 —— 这就是那个死循环"
    assert sink.failed == [original], "没落进 failed_requests，这条请求就凭空消失了"


def test_replacement_retry_counts_as_retry_not_success() -> None:
    """一次「被判为无效、要重来」的响应算成「请求成功」，汇总里的数字就是假的。"""
    buffer, stats, sink = _Buffer(), Stats(), _Sink()
    worker = _worker(buffer, stats, sink)
    original = Request("http://example.com/p/1")
    worker._retry_replacement(original, original.copy(), _resp(original))

    assert stats.get(sk.RETRY) == 1
    assert stats.get(sk.REQUEST_OK) == 0, "重试被计成了请求成功"


def test_legitimate_swap_still_works() -> None:
    """别用「把这条路径禁掉」换「不死循环」—— 正当的换号重试必须仍然有效。"""
    buffer, stats, sink = _Buffer(), Stats(), _Sink()
    worker = _worker(buffer, stats, sink)
    original = Request("http://example.com/p/1")
    original.requests_kwargs["cookies"] = {"sid": "坏号"}
    retry = original.copy()
    retry.requests_kwargs.pop("cookies", None)

    worker._retry_replacement(original, retry, _resp(original))

    assert len(buffer.put_back) == 1
    queued = buffer.put_back[0]
    assert "cookies" not in queued.requests_kwargs, "中间件换过的内容被丢掉了"
    assert queued.filter_repeat is False, "重试被去重挡住，换号就永远轮不上"
