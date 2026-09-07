"""响应体大小上限 + Content-Type 白名单（v4.4 资源边界）。

框架此前会把**任何**响应整个读进内存。实测 200MB 的响应让进程 RSS 涨 618MB
（bytes 一份、``.text`` 解码又一份）；4 个线程同时撞上 ~2.5GB，容器里就是 OOM ——
而 OOM Killer 发 SIGKILL，会绕过优雅停止把已领取的任务打丢。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request as WRequest
from werkzeug.wrappers import Response as WResponse

from mineworker import AirSpider, Request, setting
from mineworker.exceptions import ContentTypeRejectedError, ResponseTooLargeError
from mineworker.network import circuit
from mineworker.network.downloader import close_default_downloaders

CAP = 4096


@pytest.fixture(autouse=True)
def _small_cap(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(setting, "MAX_RESPONSE_SIZE", CAP)
    monkeypatch.setattr(setting, "ALLOWED_CONTENT_TYPES", [])
    yield
    close_default_downloaders()


def _serve_body(server: HTTPServer, path: str, size: int, *, declare: bool = True) -> None:
    """靶子：发 ``size`` 字节。``declare=False`` 时不报 Content-Length（分块传输）。"""

    def handler(request: WRequest) -> WResponse:
        if declare:
            return WResponse(b"x" * size, content_type="text/html")

        # 交给 werkzeug 流式发：拿到的是生成器就算不出总长度，于是不发
        # Content-Length —— 这时只能靠边读边计数
        def gen() -> Iterator[bytes]:
            for i in range(0, size, 512):
                yield b"x" * min(512, size - i)

        return WResponse(gen(), content_type="text/html")

    server.expect_request(path).respond_with_handler(handler)


# ---- 大小上限 --------------------------------------------------------
def test_under_limit_passes(httpserver: HTTPServer) -> None:
    _serve_body(httpserver, "/small", CAP // 2)
    resp = Request(httpserver.url_for("/small")).download()
    assert len(resp.content) == CAP // 2


def test_oversize_rejected_via_content_length(httpserver: HTTPServer) -> None:
    """报了 Content-Length 就在读 body 之前拒掉 —— 这一条省下的是**带宽**。"""
    _serve_body(httpserver, "/big", CAP * 4)
    with pytest.raises(ResponseTooLargeError, match="MAX_RESPONSE_SIZE"):
        Request(httpserver.url_for("/big")).download()


def test_oversize_rejected_while_streaming(httpserver: HTTPServer) -> None:
    """不报 Content-Length 时靠边读边计数拦住。

    这条比上一条重要：声明是可以缺失、也可以撒谎的（压缩响应报的就是压缩后
    大小），只信声明的实现会被整个绕过。
    """
    _serve_body(httpserver, "/chunked", CAP * 4, declare=False)
    with pytest.raises(ResponseTooLargeError):
        Request(httpserver.url_for("/chunked")).download()


def test_zero_disables_the_cap(httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "MAX_RESPONSE_SIZE", 0)
    _serve_body(httpserver, "/big2", CAP * 4)
    assert len(Request(httpserver.url_for("/big2")).download().content) == CAP * 4


# ---- Content-Type 白名单 ---------------------------------------------
def test_content_type_outside_whitelist_rejected(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setting, "ALLOWED_CONTENT_TYPES", ["text/", "application/json"])
    httpserver.expect_request("/video").respond_with_data(b"\x00\x01", content_type="video/mp4")
    with pytest.raises(ContentTypeRejectedError, match="video/mp4"):
        Request(httpserver.url_for("/video")).download()


def test_content_type_in_whitelist_passes(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setting, "ALLOWED_CONTENT_TYPES", ["text/"])
    httpserver.expect_request("/page").respond_with_data("<h1>ok</h1>", content_type="text/html")
    assert Request(httpserver.url_for("/page")).download().status_code == 200


def test_missing_content_type_is_not_judged(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 Content-Type 时放行 —— 白名单是用来挡「明确说了自己是视频」的响应，
    不是用来挡沉默的。不少站点根本不发这个头。"""
    monkeypatch.setattr(setting, "ALLOWED_CONTENT_TYPES", ["text/"])

    def handler(request: WRequest) -> WResponse:
        resp = WResponse(b"hi")
        del resp.headers["Content-Type"]
        return resp

    httpserver.expect_request("/bare").respond_with_handler(handler)
    assert Request(httpserver.url_for("/bare")).download().content == b"hi"


# ---- 与既有机制的关系 ------------------------------------------------
def test_too_large_does_not_trip_circuit_breaker() -> None:
    """响应过大是「这个 URL 太大」，不是「这个站挂了」。

    不这样区分的话，一个站上放着几个大 PDF 就能把整个域熔断 ——
    礼貌性机制会变成自伤。
    """
    assert circuit.counts_as_unhealthy(ResponseTooLargeError("太大"), None) is False


# ---- 端到端：跑一遍真爬虫，看行为对不对 ------------------------------
class _OneShot(AirSpider):
    """只发一个请求；``seen`` 收集 parse 是否真的被调用过。"""

    def __init__(self, url: str, **kw: object) -> None:
        self._url = url
        self.parsed: list[str] = []
        super().__init__(**kw)  # type: ignore[arg-type]

    def start_requests(self) -> Iterator[Request]:
        yield Request(self._url, callback=self.parse)

    def parse(self, request: Request, response: object) -> None:
        self.parsed.append(request.url)


@pytest.fixture
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "DONE_CHECK_INTERVAL", 0.04)
    monkeypatch.setattr(setting, "DONE_CHECK_TIMES", 2)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 1)
    monkeypatch.setattr(setting, "RANDOM_USER_AGENT", False)


def test_too_large_is_not_retried(httpserver: HTTPServer, _fast: None) -> None:
    """超限不重试 —— 再抓一次还是一样大，只是把同样的流量和内存再烧一遍。

    判据取自**靶子的命中次数**：框架自己说「我没重试」不算数。
    """
    hits: list[str] = []

    def handler(request: WRequest) -> WResponse:
        hits.append(request.path)
        return WResponse(b"x" * (CAP * 4), content_type="text/html")

    httpserver.expect_request("/huge").respond_with_handler(handler)
    spider = _OneShot(httpserver.url_for("/huge"))
    spider.start()

    assert len(hits) == 1, f"被打了 {len(hits)} 次 —— 超限的请求不该重试"
    assert spider.parsed == [], "超限的响应不该进 parse()"
    assert spider.scheduler.stats.get("request_failed") == 1


def test_content_type_drop_is_not_a_failure(
    httpserver: HTTPServer, _fast: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """类型白名单外的响应是**丢弃**不是失败 —— 和 robots.txt 一样不该污染失败率。"""
    monkeypatch.setattr(setting, "ALLOWED_CONTENT_TYPES", ["text/"])
    httpserver.expect_request("/clip").respond_with_data(b"\x00", content_type="video/mp4")

    spider = _OneShot(httpserver.url_for("/clip"))
    spider.start()

    assert spider.parsed == []
    assert spider.scheduler.stats.get("content_type_dropped") == 1
    assert spider.scheduler.stats.get("request_failed") == 0, "被白名单挡掉不该算失败"
