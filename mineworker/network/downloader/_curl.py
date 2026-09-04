"""基于 curl_cffi 的下载器：伪装真实浏览器的 TLS / HTTP2 指纹。

现代反爬（Cloudflare、Akamai、DataDome）看的是 TLS 握手指纹（JA3/JA4）和 HTTP/2
SETTINGS 帧，而不是 User-Agent —— 换 UA 池对它们没有意义。``curl_cffi`` 底层是
libcurl-impersonate，能复刻真实浏览器的握手，从这一层解决问题。

需 ``pip install "mineworker[curl]"``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mineworker.exceptions import RequestError
from mineworker.network.downloader._common import (
    pick_proxy,
    report_bad_proxy,
    resolve_impersonate,
    send_kwargs,
)
from mineworker.network.downloader.base import Downloader
from mineworker.network.response import Response

if TYPE_CHECKING:
    from curl_cffi.requests import Response as CurlResponse
    from curl_cffi.requests import Session
    from curl_cffi.requests.session import HttpMethod

    from mineworker.network.request import Request

    # Session 在 curl_cffi 0.16 起是泛型（按同步 / 异步响应类型参数化）
    CurlSession = Session[CurlResponse]


def _requests() -> Any:
    """延迟导入：curl_cffi 是可选依赖，没装也不该影响 import mineworker。"""
    try:
        from curl_cffi import requests
    except ModuleNotFoundError as exc:  # pragma: no cover - 取决于是否装了 extra
        raise RequestError(
            'TLS 指纹伪装需要 curl_cffi，安装：pip install "mineworker[curl]"'
        ) from exc
    return requests


class CurlDownloader(Downloader):
    """与 :class:`~mineworker.network.downloader._httpx.HttpxDownloader` 同构，
    区别只在底层换成 curl_cffi 并带上 ``impersonate``。"""

    def __init__(
        self,
        *,
        timeout: float | None = None,
        verify: bool = True,
        proxy: str | None = None,
        use_session: bool = False,
        impersonate: str | None = None,
    ) -> None:
        self._timeout = timeout
        self._verify = verify
        self._proxy = proxy
        self._use_session = use_session
        self._impersonate = impersonate
        self._session: CurlSession | None = None

    # ------------------------------------------------------------------
    def _make_session(
        self,
        proxy: str | None,
        verify: bool,
        cookies: dict[str, str] | None = None,
    ) -> CurlSession:
        kwargs: dict[str, Any] = {"verify": verify}
        if proxy:
            kwargs["proxy"] = proxy
        if cookies:
            kwargs["cookies"] = cookies
        session: CurlSession = _requests().Session(**kwargs)
        return session

    def _session_for(self, request: Request) -> tuple[CurlSession, bool, str | None]:
        """返回 (session, 用完是否关闭, 本次使用的代理)。"""
        proxy = pick_proxy(request, self._proxy)
        verify = request.requests_kwargs.get("verify", self._verify)
        cookies = request.requests_kwargs.get("cookies")
        if self._use_session and not cookies and proxy == self._proxy and verify == self._verify:
            if self._session is None:
                self._session = self._make_session(self._proxy, self._verify)
            return self._session, False, proxy
        return self._make_session(proxy, verify, cookies), True, proxy

    # ------------------------------------------------------------------
    def download(self, request: Request) -> Response:
        session, should_close, proxy = self._session_for(request)
        # curl_cffi 沿用 requests 的 allow_redirects，不是 httpx 的 follow_redirects
        kwargs = send_kwargs(request, self._timeout, redirect_key="allow_redirects")
        kwargs.setdefault("allow_redirects", True)
        impersonate = self._impersonate or resolve_impersonate(request)
        if impersonate:
            kwargs["impersonate"] = impersonate
        try:
            # method 在 Request.__init__ 里已 upper()，curl_cffi 的签名要 Literal
            resp = session.request(cast("HttpMethod", request.method), request.url, **kwargs)
        except _requests().RequestsError as exc:
            if proxy:
                report_bad_proxy(proxy)
            raise RequestError(f"下载失败 {request.method} {request.url}：{exc!r}") from exc
        finally:
            if should_close:
                session.close()
        return Response.from_curl_cffi(resp, request)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
