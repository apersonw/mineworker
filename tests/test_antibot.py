"""反爬拦截页识别。

规则的设计取向是「宁可漏报，不可误伤」，所以这里的**反例（正常页面不能被误判）
和正例一样重要**——误杀正常页面会让整个爬虫空转。
"""

from __future__ import annotations

import pytest

from mineworker import Request, setting
from mineworker.exceptions import AntiBotError, RequestError
from mineworker.network import antibot
from mineworker.network.downloader import download_request
from mineworker.network.downloader.base import Downloader
from mineworker.network.response import Response


def _resp(
    content: bytes = b"", status: int = 200, headers: dict[str, str] | None = None
) -> Response:
    return Response(
        url="https://example.com/",
        status_code=status,
        content=content,
        headers=headers or {},
    )


# ---- 正例：应当识别出来 ----------------------------------------------
def test_cloudflare_mitigated_header() -> None:
    assert antibot.detect(_resp(headers={"cf-mitigated": "challenge"})) == "cloudflare"


def test_cloudflare_challenge_script() -> None:
    body = b"<html><head><script>window.__cf_chl_opt={};</script></head><body></body></html>"
    assert antibot.detect(_resp(body, status=403)) == "cloudflare"


def test_cloudflare_challenge_platform() -> None:
    body = b"<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1'></script>"
    assert antibot.detect(_resp(body, status=503)) == "cloudflare"


def test_akamai_challenge() -> None:
    body = b"<html><script>bazadebezolkohpepadr='_abck'</script></html>"
    assert antibot.detect(_resp(body, status=403)) == "akamai"


def test_js_redirect_shell() -> None:
    body = b"<html><head><script>window.location.href='/verify?x=1'</script></head></html>"
    assert antibot.detect(_resp(body)) == "js_redirect"


def test_meta_refresh_shell() -> None:
    body = b"<html><head><meta http-equiv='refresh' content='0;url=/gate'></head></html>"
    assert antibot.detect(_resp(body)) == "js_redirect"


# ---- 反例：正常页面绝不能被误判 --------------------------------------
def test_normal_html_not_flagged() -> None:
    body = "<html><body><h1>标题</h1><p>正文</p></body></html>".encode()
    assert antibot.detect(_resp(body)) is None


def test_normal_404_not_flagged() -> None:
    assert antibot.detect(_resp(b"<html><body><p>Not Found</p></body></html>", status=404)) is None


def test_real_403_without_markers_not_flagged() -> None:
    """真的权限不足（403）但没有挑战特征，不该当成反爬。"""
    body = b"<html><body><h1>Forbidden</h1><p>You lack permission.</p></body></html>"
    assert antibot.detect(_resp(body, status=403)) is None


def test_spa_shell_with_redirect_but_long_not_flagged() -> None:
    """大页面即便含 location= 也不算空壳（正常站点到处都有跳转脚本）。"""
    body = b"<script>window.location='/x'</script>" + b"<div>" + b"x" * 4000 + b"</div>"
    assert antibot.detect(_resp(body)) is None


def test_page_with_content_and_redirect_not_flagged() -> None:
    """有正文标签就不是空壳。"""
    body = b"<html><body><p>hi</p><script>window.location='/x'</script></body></html>"
    assert antibot.detect(_resp(body)) is None


def test_json_api_not_flagged() -> None:
    assert antibot.detect(_resp(b'{"ok":true,"items":[]}')) is None


def test_empty_200_not_flagged() -> None:
    """空响应可能只是没数据，不该武断判成拦截。"""
    assert antibot.detect(_resp(b"")) is None


# ---- 与下载链路的集成 ------------------------------------------------
class _FakeDownloader(Downloader):
    def __init__(self, response: Response) -> None:
        self._response = response

    def download(self, request: Request) -> Response:
        return self._response


def test_download_request_raises_antibot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "ANTIBOT_DETECT", True)
    blocked = _resp(headers={"cf-mitigated": "challenge"})
    with pytest.raises(AntiBotError) as excinfo:
        download_request(Request("https://example.com/"), _FakeDownloader(blocked))
    assert "cloudflare" in str(excinfo.value)


def test_antibot_error_is_request_error() -> None:
    """继承关系是设计要点：ParserControl 的 except RequestError 要能自动接管重试。"""
    assert issubclass(AntiBotError, RequestError)


def test_detection_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "ANTIBOT_DETECT", False)
    blocked = _resp(headers={"cf-mitigated": "challenge"})
    resp = download_request(Request("https://example.com/"), _FakeDownloader(blocked))
    assert resp is blocked


def test_normal_response_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "ANTIBOT_DETECT", True)
    ok = _resp(b"<html><body><p>fine</p></body></html>")
    assert download_request(Request("https://example.com/"), _FakeDownloader(ok)) is ok
