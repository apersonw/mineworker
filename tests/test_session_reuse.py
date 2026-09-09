"""`USE_SESSION` 在开代理池时也要生效。

`_client_for()` 原来的复用条件是 `proxy == self._proxy` —— 拿**配置**
（下载器的固定代理，通常是 None）去比**状态**（这次实际用的代理）。
开代理池时两者永远不等，于是每个请求都新建一次 client。

实测（5 个请求，池里只有一个代理，最有利于复用的情形）：
    不开代理池   建 1 个 client
    开代理池     建 **5** 个 client —— 每次一轮全新的 TLS 握手 + 代理隧道

而压测结论正是「瓶颈是每请求建连，不是线程模型」，
所以这个开关在开代理池的部署里等于没有。
"""

from __future__ import annotations

from typing import Any

import pytest

from mineworker import setting
from mineworker.network.downloader._httpx import HttpxDownloader
from mineworker.network.request import Request


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def downloader(monkeypatch: pytest.MonkeyPatch) -> Any:
    dl = HttpxDownloader(use_session=True)
    made: list[_FakeClient] = []

    def _make(proxy: Any = None, verify: Any = True, cookies: Any = None) -> Any:
        client = _FakeClient()
        made.append(client)
        return client

    monkeypatch.setattr(dl, "_make_client", _make)
    dl.made = made  # type: ignore[attr-defined]
    return dl


def _req(url: str = "http://example.com/p/1", proxy: str | None = None) -> Request:
    request = Request(url)
    if proxy:
        request.requests_kwargs["proxy"] = proxy
    return request


def test_same_proxy_reuses_one_client(downloader: Any) -> None:
    """同一个代理的连续请求必须共用一个连接池。"""
    for _ in range(5):
        downloader._client_for(_req(proxy="http://p1:8080"))
    assert len(downloader.made) == 1, (
        f"5 个请求建了 {len(downloader.made)} 个 client —— 每次一轮全新的 TLS 握手"
    )


def test_no_proxy_still_reuses(downloader: Any) -> None:
    for _ in range(5):
        downloader._client_for(_req())
    assert len(downloader.made) == 1


def test_different_proxies_get_their_own(downloader: Any) -> None:
    """不同代理不能共用连接池 —— 那会把请求发到错的出口。"""
    downloader._client_for(_req(proxy="http://p1:8080"))
    downloader._client_for(_req(proxy="http://p2:8080"))
    downloader._client_for(_req(proxy="http://p1:8080"))
    assert len(downloader.made) == 2


def test_cache_is_bounded_and_evicted_clients_are_closed(
    downloader: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """代理池可能有上千个代理 —— 不设上限就是把性能问题换成资源泄漏。"""
    monkeypatch.setattr(setting, "SESSION_CACHE_SIZE", 3)
    for i in range(5):
        downloader._client_for(_req(proxy=f"http://p{i}:8080"))

    assert len(downloader._clients) == 3, "缓存没有上限"
    closed = [c for c in downloader.made if c.closed]
    assert len(closed) == 2, "换出的 client 没关掉 —— 连接和 fd 就泄漏了"


def test_close_closes_every_client(downloader: Any) -> None:
    for i in range(3):
        downloader._client_for(_req(proxy=f"http://p{i}:8080"))
    downloader.close()
    assert all(c.closed for c in downloader.made), "close() 漏掉了缓存里的连接池"
    assert downloader._clients == {}


def test_request_with_cookies_gets_a_throwaway_client(downloader: Any) -> None:
    """带 cookies 是每请求的状态，不能污染共用的连接池。"""
    request = _req()
    request.requests_kwargs["cookies"] = {"sid": "x"}
    _client, should_close, _proxy = downloader._client_for(request)
    assert should_close is True
    assert downloader._clients == {}
