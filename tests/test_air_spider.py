from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Request as WRequest
from werkzeug import Response as WResponse

from mineworker import AirSpider, Request, setting
from mineworker.exceptions import NotRetryError, ValidationError


@pytest.fixture(autouse=True)
def _fast_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "DONE_CHECK_INTERVAL", 0.04)
    monkeypatch.setattr(setting, "DONE_CHECK_TIMES", 2)
    monkeypatch.setattr(setting, "BUFFER_FLUSH_INTERVAL", 0.02)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 3)
    monkeypatch.setattr(setting, "RANDOM_USER_AGENT", False)


def _setup_pages(server: HTTPServer, pages: int, per_page: int) -> None:
    for page in range(1, pages + 1):
        lis = "".join(f"<li>p{page}i{i}</li>" for i in range(per_page))
        nxt = f'<a id="next" href="/page/{page + 1}">n</a>' if page < pages else ""
        server.expect_request(f"/page/{page}").respond_with_data(
            f"<html><body><ul>{lis}</ul>{nxt}</body></html>",
            content_type="text/html; charset=utf-8",
        )


class CrawlSpider(AirSpider):
    def __init__(self, start_url: str, **kw: Any) -> None:
        self._start_url = start_url
        self.items: list[Any] = []
        self.started = False
        self.ended = False
        super().__init__(item_handler=self.items.extend, **kw)

    def start_requests(self) -> Iterator[Request]:
        yield Request(self._start_url, callback=self.parse_page)

    def parse_page(self, request: Request, response: Any) -> Iterator[Any]:
        for text in response.css("li::text").getall():
            yield {"v": text}
        nxt = response.css("a#next::attr(href)").get()
        if nxt:
            yield Request(response.urljoin(nxt), callback=self.parse_page)

    def start_callback(self) -> None:
        self.started = True

    def end_callback(self) -> None:
        self.ended = True


def _wait_threads(baseline: int, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while threading.active_count() > baseline and time.monotonic() < deadline:
        time.sleep(0.05)


# ----------------------------------------------------------------------
def test_crawl_pagination_to_completion(httpserver: HTTPServer) -> None:
    _setup_pages(httpserver, pages=3, per_page=4)
    spider = CrawlSpider(httpserver.url_for("/page/1"))
    spider.start()

    assert spider.started is True
    assert spider.ended is True
    assert sorted(d["v"] for d in spider.items) == sorted(
        f"p{p}i{i}" for p in (1, 2, 3) for i in range(4)
    )
    assert spider.scheduler.stats.get("request_ok") == 3
    assert spider.scheduler.stats.get("item") == 12


def test_no_thread_leak(httpserver: HTTPServer) -> None:
    _setup_pages(httpserver, pages=2, per_page=2)
    baseline = threading.active_count()
    CrawlSpider(httpserver.url_for("/page/1")).start()
    _wait_threads(baseline)
    assert threading.active_count() == baseline


def test_retry_then_success(httpserver: HTTPServer) -> None:
    calls = {"n": 0}

    def handler(_: WRequest) -> WResponse:
        calls["n"] += 1
        if calls["n"] < 3:
            return WResponse("boom", status=500)
        return WResponse("<html><body><li>ok</li></body></html>", content_type="text/html")

    httpserver.expect_request("/x").respond_with_handler(handler)

    class S(CrawlSpider):
        def validate(self, request: Request, response: Any) -> bool:
            if response.status_code != 200:
                raise ValidationError(str(response.status_code))
            return True

    spider = S(httpserver.url_for("/x"))
    spider.start()

    assert spider.items == [{"v": "ok"}]
    assert spider.scheduler.stats.get("retry") == 2
    assert calls["n"] == 3


def test_failed_request_hook(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/bad").respond_with_data("nope", status=500)

    class S(AirSpider):
        __custom_setting__ = {"SPIDER_MAX_RETRY_TIMES": 2}

        def __init__(self, url: str, **kw: Any) -> None:
            self._url = url
            self.failed: list[str] = []
            super().__init__(**kw)

        def start_requests(self) -> Iterator[Request]:
            yield Request(self._url, callback=self.parse)

        def validate(self, request: Request, response: Any) -> bool:
            if response.status_code != 200:
                raise ValidationError("bad")
            return True

        def parse(self, request: Request, response: Any) -> None:
            return None

        def failed_request(self, request: Request, response: Any) -> None:
            self.failed.append(request.url)
            return None

    spider = S(httpserver.url_for("/bad"))
    spider.start()

    assert spider.failed == [httpserver.url_for("/bad")]
    assert spider.scheduler.stats.get("request_failed") == 1
    assert spider.scheduler.stats.get("retry") == 2


def test_duplicate_requests_are_deduped(httpserver: HTTPServer) -> None:
    hits = {"n": 0}

    def handler(_: WRequest) -> WResponse:
        hits["n"] += 1
        return WResponse("<html><body><li>x</li></body></html>", content_type="text/html")

    httpserver.expect_request("/dup").respond_with_handler(handler)

    class S(CrawlSpider):
        def start_requests(self) -> Iterator[Request]:
            for _ in range(5):
                yield Request(self._start_url, callback=self.parse_page)

    spider = S(httpserver.url_for("/dup"))
    spider.start()

    assert hits["n"] == 1
    assert spider.items == [{"v": "x"}]
    assert spider.scheduler.stats.get("dedup_dropped") == 4


def test_download_midware_modifies_request(httpserver: HTTPServer) -> None:
    seen = {}

    def handler(request: WRequest) -> WResponse:
        seen["hdr"] = request.headers.get("X-From-Midware")
        return WResponse("<html><body></body></html>", content_type="text/html")

    httpserver.expect_request("/m").respond_with_handler(handler)

    class S(AirSpider):
        def __init__(self, url: str, **kw: Any) -> None:
            self._url = url
            super().__init__(**kw)

        def start_requests(self) -> Iterator[Request]:
            yield Request(self._url, callback=self.parse)

        def parse(self, request: Request, response: Any) -> None:
            return None

        def download_midware(self, request: Request) -> Request:
            request.requests_kwargs.setdefault("headers", {})["X-From-Midware"] = "yes"
            return request

    S(httpserver.url_for("/m")).start()
    assert seen["hdr"] == "yes"


def test_custom_setting_controls_thread_count(httpserver: HTTPServer) -> None:
    _setup_pages(httpserver, pages=1, per_page=1)

    class S(CrawlSpider):
        __custom_setting__ = {"SPIDER_THREAD_COUNT": 2}

    spider = S(httpserver.url_for("/page/1"))
    assert spider.scheduler._thread_count == 2
    spider.start()
    assert spider.ended is True


def test_validate_false_drops_without_retry(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v").respond_with_data(
        "<html><body><li>x</li></body></html>", content_type="text/html"
    )

    class S(CrawlSpider):
        def validate(self, request: Request, response: Any) -> bool:
            return False

    spider = S(httpserver.url_for("/v"))
    spider.start()

    assert spider.items == []
    assert spider.scheduler.stats.get("dropped") == 1
    assert spider.scheduler.stats.get("retry") == 0


def test_not_retry_error_in_parse_drops(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/n").respond_with_data(
        "<html><body></body></html>", content_type="text/html"
    )

    class S(CrawlSpider):
        def parse_page(self, request: Request, response: Any) -> Iterator[Any]:
            raise NotRetryError("stop here")

    spider = S(httpserver.url_for("/n"))
    spider.start()

    assert spider.scheduler.stats.get("dropped") == 1
    assert spider.scheduler.stats.get("retry") == 0
    assert spider.ended is True


def test_default_parse_used_when_no_callback(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/d").respond_with_data(
        "<html><body><li>hi</li></body></html>", content_type="text/html"
    )
    collected: list[Any] = []

    class S(AirSpider):
        def __init__(self, url: str, **kw: Any) -> None:
            self._url = url
            super().__init__(item_handler=collected.extend, **kw)

        def start_requests(self) -> Iterator[Request]:
            yield Request(self._url)  # 不指定 callback

        def parse(self, request: Request, response: Any) -> Iterator[Any]:
            for text in response.css("li::text").getall():
                yield {"v": text}

    S(httpserver.url_for("/d")).start()
    assert collected == [{"v": "hi"}]


def test_exception_request_hook_called_on_retry(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/e").respond_with_data("err", status=500)
    seen: list[int] = []

    class S(AirSpider):
        __custom_setting__ = {"SPIDER_MAX_RETRY_TIMES": 2}

        def __init__(self, url: str, **kw: Any) -> None:
            self._url = url
            super().__init__(**kw)

        def start_requests(self) -> Iterator[Request]:
            yield Request(self._url, callback=self.parse)

        def validate(self, request: Request, response: Any) -> bool:
            if response.status_code != 200:
                raise ValidationError("bad")
            return True

        def parse(self, request: Request, response: Any) -> None:
            return None

        def exception_request(
            self, request: Request, response: Any, exception: BaseException
        ) -> None:
            seen.append(request.retry_times)

    S(httpserver.url_for("/e")).start()
    assert seen == [1, 2]  # 每次重试前调用一次


def test_debug_forces_single_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    class S(CrawlSpider):
        pass

    spider = S("http://127.0.0.1:1/never", debug=True)
    assert spider.scheduler._thread_count == 1
    assert setting.DEBUG is True


def test_stop_exits_and_dumps_unfinished(
    httpserver: HTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 1)

    def slow(_: WRequest) -> WResponse:
        time.sleep(0.15)
        return WResponse("<html></html>", content_type="text/html")

    httpserver.expect_request("/s").respond_with_handler(slow)
    url = httpserver.url_for("/s")

    class S(AirSpider):
        def start_requests(self) -> Iterator[Request]:
            for i in range(30):
                yield Request(f"{url}?i={i}", callback=self.parse)

        def parse(self, request: Request, response: Any) -> None:
            return None

    spider = S()
    timer = threading.Timer(0.25, spider.stop)
    timer.start()
    spider.start()
    timer.cancel()

    dump = tmp_path / "failed_requests.jsonl"
    assert dump.exists()
    assert len(dump.read_text(encoding="utf-8").strip().splitlines()) > 0
