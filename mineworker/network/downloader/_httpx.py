"""基于 httpx 的同步下载器。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from mineworker import setting
from mineworker.exceptions import RequestError
from mineworker.network.downloader.base import Downloader
from mineworker.network.proxy_pool import get_proxy_pool
from mineworker.network.response import Response
from mineworker.network.user_agent import get_random_user_agent

if TYPE_CHECKING:
    from mineworker.network.request import Request

# httpx 0.28 用 follow_redirects 取代 requests 的 allow_redirects；
# cookies 放到 client 上（httpx 已弃用 per-request cookies）
_CLIENT_ONLY_KEYS = frozenset({"verify", "proxy", "proxies", "cookies"})


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
        if proxy:
            kwargs["proxy"] = proxy
        if cookies:
            kwargs["cookies"] = cookies
        return httpx.Client(**kwargs)

    def _pick_proxy(self, request: Request) -> str | None:
        rk = request.requests_kwargs
        explicit = rk.get("proxy") or rk.get("proxies") or self._proxy
        if explicit:
            return explicit
        pool = get_proxy_pool()
        return pool.get_proxy() if pool is not None else None

    def _client_for(self, request: Request) -> tuple[httpx.Client, bool, str | None]:
        """返回 (client, 用完是否关闭, 本次使用的代理)。"""
        proxy = self._pick_proxy(request)
        verify = request.requests_kwargs.get("verify", self._verify)
        cookies = request.requests_kwargs.get("cookies")
        if self._use_session and not cookies and proxy == self._proxy and verify == self._verify:
            if self._client is None:
                self._client = self._make_client(self._proxy, self._verify)
            return self._client, False, proxy
        return self._make_client(proxy, verify, cookies), True, proxy

    def _send_kwargs(self, request: Request) -> dict[str, Any]:
        kwargs = {k: v for k, v in request.requests_kwargs.items() if k not in _CLIENT_ONLY_KEYS}
        if "allow_redirects" in kwargs:
            kwargs["follow_redirects"] = kwargs.pop("allow_redirects")
        if "timeout" not in kwargs:
            kwargs["timeout"] = (
                self._timeout if self._timeout is not None else setting.REQUEST_TIMEOUT
            )

        want_ua = request.random_user_agent
        if want_ua is None:
            want_ua = setting.RANDOM_USER_AGENT
        headers = dict(kwargs.get("headers") or {})
        if want_ua and not any(k.lower() == "user-agent" for k in headers):
            headers["User-Agent"] = get_random_user_agent()
        if headers:
            kwargs["headers"] = headers
        return kwargs

    # ------------------------------------------------------------------
    def download(self, request: Request) -> Response:
        client, should_close, proxy = self._client_for(request)
        try:
            resp = client.request(request.method, request.url, **self._send_kwargs(request))
        except httpx.HTTPError as exc:
            if proxy:
                pool = get_proxy_pool()
                if pool is not None:
                    pool.report_bad(proxy)
            raise RequestError(f"下载失败 {request.method} {request.url}：{exc!r}") from exc
        finally:
            if should_close:
                client.close()
        return Response.from_httpx(resp, request)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
