from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from pytest_httpserver import HTTPServer

from mineworker import AirSpider, Request, setting
from mineworker.exceptions import RequestError
from mineworker.network.downloader import close_default_downloaders
from mineworker.network.downloader._playwright import PlaywrightDownloader

pytestmark = pytest.mark.render

JS_PAGE = """<html><body>
<div id="app">loading</div>
<script>
setTimeout(function () {
  document.getElementById('app').textContent = 'rendered';
  var ul = document.createElement('ul');
  ['A', 'B', 'C'].forEach(function (t) {
    var li = document.createElement('li');
    li.className = 'row';
    li.textContent = t;
    ul.appendChild(li);
  });
  document.body.appendChild(ul);
}, 60);
</script></body></html>"""


@pytest.fixture(autouse=True)
def _cleanup_registry() -> Iterator[None]:
    yield
    close_default_downloaders()


@pytest.fixture(scope="session")
def chromium_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
    except Exception:
        return False
    return True


@pytest.fixture(autouse=True)
def _require_chromium(chromium_ok: bool) -> None:
    if not chromium_ok:
        pytest.skip("chromium 不可用（playwright install chromium）")


@pytest.fixture(scope="module")
def pool(chromium_ok: bool) -> Iterator[PlaywrightDownloader]:
    if not chromium_ok:
        pytest.skip("chromium 不可用")
    downloader = PlaywrightDownloader({"pool_size": 2})
    yield downloader
    downloader.close()


@pytest.fixture
def js_url(httpserver: HTTPServer) -> str:
    httpserver.expect_request("/js").respond_with_data(
        JS_PAGE, content_type="text/html; charset=utf-8"
    )
    return httpserver.url_for("/js")


# ----------------------------------------------------------------------
def test_render_executes_js(pool: PlaywrightDownloader, js_url: str) -> None:
    resp = pool.download(Request(js_url, render=True, wait_for="ul li.row"))
    assert resp.status_code == 200
    assert "rendered" in resp.text
    assert resp.css("li.row::text").getall() == ["A", "B", "C"]
    assert resp.request is not None


def test_without_render_js_is_not_executed(js_url: str) -> None:
    resp = Request(js_url).download()  # httpx，不渲染
    assert "loading" in resp.text
    assert resp.css("li.row::text").getall() == []


def test_render_time_waits_for_late_mutation(
    pool: PlaywrightDownloader, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/late").respond_with_data(
        "<html><body><p id=x>early</p><script>"
        "setTimeout(function(){document.getElementById('x').textContent='late';}, 250);"
        "</script></body></html>",
        content_type="text/html",
    )
    url = httpserver.url_for("/late")
    assert "early" in pool.download(Request(url, render=True, render_time=0)).text
    assert "late" in pool.download(Request(url, render=True, render_time=0.6)).text


def test_render_script_runs_in_browser(pool: PlaywrightDownloader, httpserver: HTTPServer) -> None:
    httpserver.expect_request("/btn").respond_with_data(
        '<html><body><button id="b" '
        "onclick=\"document.body.setAttribute('data-clicked','yes')\">go</button>"
        "</body></html>",
        content_type="text/html",
    )

    def script(page: Any) -> None:
        page.click("#b")

    resp = pool.download(Request(httpserver.url_for("/btn"), render=True, render_script=script))
    assert 'data-clicked="yes"' in resp.text


def test_error_becomes_request_error(pool: PlaywrightDownloader) -> None:
    with pytest.raises(RequestError, match="渲染失败"):
        pool.download(Request("http://127.0.0.1:1/nope", render=True))


def test_pool_close_stops_render_threads(js_url: str) -> None:
    baseline = threading.active_count()
    downloader = PlaywrightDownloader({"pool_size": 2})
    downloader.download(Request(js_url, render=True))
    assert threading.active_count() >= baseline + 2

    downloader.close()
    deadline = time.monotonic() + 5
    while threading.active_count() > baseline and time.monotonic() < deadline:
        time.sleep(0.05)
    assert threading.active_count() == baseline


def test_spider_with_render_end_to_end(
    chromium_ok: bool, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not chromium_ok:
        pytest.skip("chromium 不可用")
    monkeypatch.setattr(setting, "DONE_CHECK_INTERVAL", 0.05)
    monkeypatch.setattr(setting, "DONE_CHECK_TIMES", 2)
    monkeypatch.setattr(setting, "BUFFER_FLUSH_INTERVAL", 0.02)
    monkeypatch.setattr(setting, "WEBDRIVER", {**setting.WEBDRIVER, "pool_size": 2})
    httpserver.expect_request("/js").respond_with_data(
        JS_PAGE, content_type="text/html; charset=utf-8"
    )
    base = httpserver.url_for("/js")

    class S(AirSpider):
        def start_requests(self) -> Iterator[Request]:
            for i in range(3):
                yield Request(f"{base}?i={i}", render=True, wait_for="li.row", callback=self.parse)

        def parse(self, request: Request, response: Any) -> Iterator[Any]:
            for text in response.css("li.row::text").getall():
                yield {"v": text}

    items: list[Any] = []
    S(item_handler=items.extend, thread_count=3).start()
    assert sorted(d["v"] for d in items) == ["A", "A", "A", "B", "B", "B", "C", "C", "C"]
