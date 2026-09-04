from __future__ import annotations

import threading
from collections.abc import Iterator

import httpx
import pytest
import respx

from mineworker import Request, RequestError, setting
from mineworker.network.downloader import close_default_downloaders, get_default_downloader
from mineworker.network.downloader._async_httpx import AsyncHttpxDownloader
from mineworker.network.user_agent import USER_AGENTS


@pytest.fixture
def downloader() -> Iterator[AsyncHttpxDownloader]:
    dl = AsyncHttpxDownloader(concurrency=8)
    try:
        yield dl
    finally:
        dl.close()


@respx.mock
def test_download_returns_response(downloader: AsyncHttpxDownloader) -> None:
    respx.get("https://example.com/").mock(return_value=httpx.Response(200, html="<h1>Hi</h1>"))
    resp = downloader.download(Request("https://example.com/"))
    assert resp.status_code == 200
    assert resp.ok
    assert resp.xpath("//h1/text()").get() == "Hi"
    assert resp.request is not None


@respx.mock
def test_params_headers_and_random_ua(downloader: AsyncHttpxDownloader) -> None:
    route = respx.get("https://example.com/s").mock(return_value=httpx.Response(200))
    downloader.download(Request("https://example.com/s", params={"q": "x"}, headers={"X-T": "1"}))
    sent = route.calls.last.request
    assert sent.url.params["q"] == "x"
    assert sent.headers["x-t"] == "1"
    assert sent.headers["user-agent"] in USER_AGENTS


@respx.mock
def test_random_ua_disabled(downloader: AsyncHttpxDownloader) -> None:
    route = respx.get("https://example.com/").mock(return_value=httpx.Response(200))
    downloader.download(Request("https://example.com/", random_user_agent=False))
    assert route.calls.last.request.headers["user-agent"] not in USER_AGENTS


@respx.mock
def test_redirects_followed_by_default(downloader: AsyncHttpxDownloader) -> None:
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/b"})
    )
    respx.get("https://example.com/b").mock(return_value=httpx.Response(200, text="B"))
    resp = downloader.download(Request("https://example.com/a"))
    assert resp.text == "B"
    assert resp.history == ["https://example.com/a"]


@respx.mock
def test_allow_redirects_false_stops_at_302(downloader: AsyncHttpxDownloader) -> None:
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/b"})
    )
    resp = downloader.download(Request("https://example.com/a", allow_redirects=False))
    assert resp.status_code == 302


@respx.mock
def test_network_error_becomes_request_error(downloader: AsyncHttpxDownloader) -> None:
    respx.get("https://example.com/").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(RequestError, match="下载失败"):
        downloader.download(Request("https://example.com/"))


@respx.mock
def test_many_worker_threads_share_one_loop(downloader: AsyncHttpxDownloader) -> None:
    respx.get("https://example.com/").mock(return_value=httpx.Response(200, text="ok"))
    results: list[str] = []

    def hit() -> None:
        results.append(downloader.download(Request("https://example.com/")).text)

    workers = [threading.Thread(target=hit) for _ in range(12)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=5)

    assert results == ["ok"] * 12
    loop_threads = [t for t in threading.enumerate() if t.name == "async-downloader"]
    assert len(loop_threads) == 1


def test_close_is_idempotent() -> None:
    dl = AsyncHttpxDownloader(concurrency=2)
    dl.close()
    dl.close()  # 不抛
    assert not any(t.name == "async-downloader" and t.is_alive() for t in threading.enumerate())


@respx.mock
def test_get_default_downloader_uses_async_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "DOWNLOADER_ASYNC", True)
    respx.get("https://example.com/").mock(return_value=httpx.Response(200, text="ok"))
    try:
        dl = get_default_downloader(Request("https://example.com/"))
        assert isinstance(dl, AsyncHttpxDownloader)
        assert dl.download(Request("https://example.com/")).text == "ok"
    finally:
        close_default_downloaders()
