"""基于 httpx 的同步下载器。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from mineworker import setting
from mineworker.exceptions import RequestError
from mineworker.network.downloader._common import (
    CLIENT_ONLY_KEYS,
    pick_proxy,
    report_bad_proxy,
    send_kwargs,
)
from mineworker.network.downloader.base import Downloader
from mineworker.network.response import Response

if TYPE_CHECKING:
    from mineworker.network.request import Request

# 保留旧名（曾是本模块私有常量）
_CLIENT_ONLY_KEYS = CLIENT_ONLY_KEYS


class HttpxDownloader(Downloader):
    def __init__(
        self,
        *,
        timeout: float | None = None,
        verify: bool = True,
        proxy: str | None = None,
        use_session: bool = False,
    ) -> None:
        self._timeout = timeout
        self._verify = verify
        self._proxy = proxy
        self._use_session = use_session
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------------
    def _make_client(
        self,
        proxy: str | None,
        verify: bool,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Client:
        kwargs: dict[str, Any] = {"follow_redirects": True, "verify": verify}
        if setting.HTTPX_HTTP2:
            kwargs["http2"] = True
        if proxy:
            kwargs["proxy"] = proxy
        if cookies:
            kwargs["cookies"] = cookies
        return httpx.Client(**kwargs)

    def _client_for(self, request: Request) -> tuple[httpx.Client, bool, str | None]:
        """返回 (client, 用完是否关闭, 本次使用的代理)。"""
        proxy = pick_proxy(request, self._proxy)
        verify = request.requests_kwargs.get("verify", self._verify)
        cookies = request.requests_kwargs.get("cookies")
        if self._use_session and not cookies and proxy == self._proxy and verify == self._verify:
            if self._client is None:
                self._client = self._make_client(self._proxy, self._verify)
            return self._client, False, proxy
        return self._make_client(proxy, verify, cookies), True, proxy

    # ------------------------------------------------------------------
    def download(self, request: Request) -> Response:
        client, should_close, proxy = self._client_for(request)
        try:
            resp = client.request(
                request.method, request.url, **send_kwargs(request, self._timeout)
            )
        except httpx.HTTPError as exc:
            if proxy:
                report_bad_proxy(proxy)
            raise RequestError(f"下载失败 {request.method} {request.url}：{exc!r}") from exc
        finally:
            if should_close:
                client.close()
        return Response.from_httpx(resp, request)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
