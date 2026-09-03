"""对真实 socket 跑一遍完整链路（Request -> HttpxDownloader -> Response）。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pytest_httpserver import HTTPServer

from mineworker import Request
from mineworker.network.downloader import close_default_downloaders


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    yield
    close_default_downloaders()


def test_end_to_end_html(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/list").respond_with_data(
        "<html><body><a class='t' href='/item/1'>一</a>"
        "<a class='t' href='/item/2'>二</a></body></html>",
        content_type="text/html; charset=utf-8",
    )
    resp = Request(httpserver.url_for("/list")).download()

    assert resp.status_code == 200
    assert resp.ok
    hrefs = resp.xpath('//a[@class="t"]/@href').getall()
    assert hrefs == ["/item/1", "/item/2"]
    assert resp.urljoin(hrefs[0]) == httpserver.url_for("/item/1")
    assert resp.css("a.t::text").getall() == ["一", "二"]


def test_end_to_end_json_and_query(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/api", query_string="page=2").respond_with_json(
        {"items": [1, 2, 3], "page": 2}
    )
    resp = Request(httpserver.url_for("/api"), params={"page": "2"}).download()
    assert resp.json() == {"items": [1, 2, 3], "page": 2}


def test_end_to_end_session_reuse(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/ping").respond_with_data("pong")
    url = httpserver.url_for("/ping")
    assert Request(url, use_session=True).download().text == "pong"
    assert Request(url, use_session=True).download().text == "pong"
