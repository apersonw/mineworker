from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from mineworker import Request, RequestError, setting
from mineworker.network.downloader import (
    HttpxDownloader,
    close_default_downloaders,
    download_request,
    get_default_downloader,
)
from mineworker.network.user_agent import USER_AGENTS


@pytest.fixture(autouse=True)
def _close_downloaders() -> Iterator[None]:
    yield
    close_default_downloaders()


@respx.mock
def test_download_returns_response() -> None:
    respx.get("https://example.com/").mock(return_value=httpx.Response(200, html="<h1>Hi</h1>"))
    resp = Request("https://example.com/").download()
    assert resp.status_code == 200
    assert resp.ok
    assert resp.xpath("//h1/text()").get() == "Hi"
    assert resp.request is not None


@respx.mock
def test_query_and_headers_are_sent() -> None:
    route = respx.get("https://example.com/s").mock(return_value=httpx.Response(200))
    Request("https://example.com/s", params={"q": "x"}, headers={"X-Test": "1"}).download()
    sent = route.calls.last.request
    assert sent.url.params["q"] == "x"
    assert sent.headers["x-test"] == "1"


@respx.mock
def test_random_user_agent_injected_by_default() -> None:
    route = respx.get("https://example.com/").mock(return_value=httpx.Response(200))
    Request("https://example.com/").download()
    assert route.calls.last.request.headers["user-agent"] in USER_AGENTS


@respx.mock
def test_explicit_user_agent_not_overridden() -> None:
    route = respx.get("https://example.com/").mock(return_value=httpx.Response(200))
    Request("https://example.com/", headers={"User-Agent": "mine/1.0"}).download()
    assert route.calls.last.request.headers["user-agent"] == "mine/1.0"


@respx.mock
def test_random_user_agent_disabled() -> None:
    route = respx.get("https://example.com/").mock(return_value=httpx.Response(200))
    Request("https://example.com/", random_user_agent=False).download()
    assert route.calls.last.request.headers["user-agent"] not in USER_AGENTS


@respx.mock
def test_allow_redirects_false_stops_at_302() -> None:
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/b"})
    )
    resp = Request("https://example.com/a", allow_redirects=False).download()
    assert resp.status_code == 302


@respx.mock
def test_redirects_followed_by_default() -> None:
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/b"})
    )
    respx.get("https://example.com/b").mock(return_value=httpx.Response(200, text="B"))
    resp = Request("https://example.com/a").download()
    assert resp.status_code == 200
    assert resp.text == "B"
    assert resp.history == ["https://example.com/a"]


@respx.mock
def test_network_error_becomes_request_error() -> None:
    respx.get("https://example.com/").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(RequestError, match="下载失败"):
        Request("https://example.com/").download()


@respx.mock
def test_session_downloader_reuses_client() -> None:
    respx.get("https://example.com/").mock(return_value=httpx.Response(200))
    req = Request("https://example.com/", use_session=True)
    downloader = get_default_downloader(req)
    assert isinstance(downloader, HttpxDownloader)
    download_request(req)
    download_request(req)
    assert downloader._client is not None


def test_render_true_routes_to_playwright_downloader() -> None:
    from mineworker.network.downloader._playwright import PlaywrightDownloader

    dl = get_default_downloader(Request("https://example.com/", render=True))
    assert isinstance(dl, PlaywrightDownloader)


@respx.mock
def test_explicit_downloader_and_context_manager() -> None:
    respx.get("https://example.com/").mock(return_value=httpx.Response(200, text="ok"))
    with HttpxDownloader(use_session=True) as dl:
        resp = Request("https://example.com/").download(dl)
        assert resp.text == "ok"
        assert dl._client is not None
    assert dl._client is None  # __exit__ 已 close


# ---- USE_SESSION 曾是死配置（benchmark 里两行数字一模一样才暴露）------------
def test_use_session_setting_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    """`setting.USE_SESSION` 必须真的生效 —— 它一度只是个装饰品。"""
    from mineworker.network.downloader import get_default_downloader

    monkeypatch.setattr(setting, "USE_SESSION", True)
    monkeypatch.setattr(setting, "DOWNLOADER_ASYNC", False)
    monkeypatch.setattr(setting, "DOWNLOADER_IMPERSONATE", "")
    dl = get_default_downloader(Request("http://x/"))
    assert isinstance(dl, HttpxDownloader)
    assert dl._use_session is True


def test_request_use_session_overrides_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from mineworker.network.downloader import get_default_downloader

    monkeypatch.setattr(setting, "USE_SESSION", True)
    monkeypatch.setattr(setting, "DOWNLOADER_ASYNC", False)
    monkeypatch.setattr(setting, "DOWNLOADER_IMPERSONATE", "")
    dl = get_default_downloader(Request("http://x/", use_session=False))
    assert dl._use_session is False


def test_use_session_default_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from mineworker.network.downloader import get_default_downloader

    monkeypatch.setattr(setting, "DOWNLOADER_ASYNC", False)
    monkeypatch.setattr(setting, "DOWNLOADER_IMPERSONATE", "")
    assert get_default_downloader(Request("http://x/"))._use_session is False


# ---- SSL context 缓存：每请求新建 Client 的最大单项开销 --------------------
def test_ssl_context_is_cached() -> None:
    """同一个 verify 值必须拿到同一个 SSLContext —— 否则每请求 ~33ms 白花。"""
    from mineworker.network.downloader._common import ssl_context_for

    assert ssl_context_for(True) is ssl_context_for(True)


def test_ssl_context_passthrough_for_uncacheable() -> None:
    import ssl as _ssl

    from mineworker.network.downloader._common import ssl_context_for

    assert ssl_context_for(False) is False
    ctx = _ssl.create_default_context()
    assert ssl_context_for(ctx) is ctx
