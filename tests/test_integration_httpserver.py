"""对真实 socket 跑一遍完整链路（Request -> HttpxDownloader -> Response）。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pytest_httpserver import HTTPServer

from conn_counting_server import ConnCountingServer
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


def test_end_to_end_session_reuse() -> None:
    """开 session 时，连着两个请求只该开**一条** TCP 连接。

    ⚠️ 这条用例原来只断言 body 是 "pong" —— **把连接复用整个关掉它照样绿**
    （实测：删掉 `_client_for` 里走 session 的分支，本条仍 1 passed）。
    名字承诺的东西，函数体里一个字都没验。

    换成服务端数连接：判据由靶子给出，框架怎么想都不影响它。
    完整的三个下载器覆盖在 `test_connection_reuse_truth.py`。
    """
    with ConnCountingServer() as srv:
        assert Request(srv.url, use_session=True).download().status_code == 200
        assert Request(srv.url, use_session=True).download().status_code == 200
        assert srv.requests == 2
        assert srv.connections == 1, f"两个请求开了 {srv.connections} 条连接，没有复用"
