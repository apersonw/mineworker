from __future__ import annotations

import httpx
import pytest
import respx

from mineworker import Request, RequestError, setting
from mineworker.network.downloader import close_default_downloaders
from mineworker.network.downloader._httpx import HttpxDownloader
from mineworker.network.proxy_pool import ProxyPool, close_proxy_pool, get_proxy_pool
from mineworker.network.proxy_pool.api import ApiProxyPool


@pytest.fixture(autouse=True)
def _cleanup() -> None:
    yield
    close_proxy_pool()
    close_default_downloaders()


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "PROXY_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(setting, "PROXY_MAX_USE_TIMES", 100)


def test_parse_plain_and_json() -> None:
    assert ApiProxyPool._parse("1.1.1.1:80\n2.2.2.2:80") == ["1.1.1.1:80", "2.2.2.2:80"]
    assert ApiProxyPool._parse('["3.3.3.3:80", "4.4.4.4:80"]') == [
        "3.3.3.3:80",
        "4.4.4.4:80",
    ]


@respx.mock
def test_api_pool_fetches_and_rotates() -> None:
    respx.get("https://proxy.api/list").mock(
        return_value=httpx.Response(200, text="1.1.1.1:8000\n2.2.2.2:8000")
    )
    pool = ApiProxyPool("https://proxy.api/list")
    seen = {pool.get_proxy() for _ in range(6)}
    assert seen == {"http://1.1.1.1:8000", "http://2.2.2.2:8000"}


@respx.mock
def test_report_bad_excludes_proxy() -> None:
    respx.get("https://p/l").mock(return_value=httpx.Response(200, text="1.1.1.1:80\n2.2.2.2:80"))
    pool = ApiProxyPool("https://p/l")
    pool.get_proxy()
    pool.report_bad("http://1.1.1.1:80")
    for _ in range(5):
        assert pool.get_proxy() != "http://1.1.1.1:80"


@respx.mock
def test_get_proxy_pool_singleton_and_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "PROXY_ENABLE", False)
    assert get_proxy_pool() is None

    monkeypatch.setattr(setting, "PROXY_ENABLE", True)
    monkeypatch.setattr(setting, "PROXY_EXTRACT_API", "https://p/l")
    respx.get("https://p/l").mock(return_value=httpx.Response(200, text="9.9.9.9:80"))
    first = get_proxy_pool()
    assert first is get_proxy_pool()
    close_proxy_pool()
    assert get_proxy_pool() is not first


class _SpyPool(ProxyPool):
    def __init__(self) -> None:
        self.handed: list[str] = []
        self.bad: list[str] = []

    def get_proxy(self) -> str:
        self.handed.append("http://p:1")
        return "http://p:1"

    def report_bad(self, proxy: str) -> None:
        self.bad.append(proxy)


@respx.mock
def test_downloader_uses_pool_and_reports_bad(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _SpyPool()
    monkeypatch.setattr("mineworker.network.downloader._common.get_proxy_pool", lambda: spy)
    respx.get("https://x.test/").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(RequestError):
        HttpxDownloader().download(Request("https://x.test/"))
    assert spy.handed == ["http://p:1"]
    assert spy.bad == ["http://p:1"]


@respx.mock
def test_explicit_request_proxy_skips_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _SpyPool()
    monkeypatch.setattr("mineworker.network.downloader._common.get_proxy_pool", lambda: spy)
    monkeypatch.setattr(setting, "RANDOM_USER_AGENT", False)
    respx.get("https://x.test/").mock(return_value=httpx.Response(200, text="ok"))
    resp = HttpxDownloader().download(Request("https://x.test/", proxy="http://explicit:9"))
    assert resp.text == "ok"
    assert spy.handed == []
