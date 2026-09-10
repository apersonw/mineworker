"""基于 curl_cffi 的下载器：伪装真实浏览器的 TLS / HTTP2 指纹。

现代反爬（Cloudflare、Akamai、DataDome）看的是 TLS 握手指纹（JA3/JA4）和 HTTP/2
SETTINGS 帧，而不是 User-Agent —— 换 UA 池对它们没有意义。``curl_cffi`` 底层是
libcurl-impersonate，能复刻真实浏览器的握手，从这一层解决问题。

需 ``pip install "mineworker[curl]"``。
"""

from __future__ import annotations

import contextlib
from http.cookiejar import CookieJar
from typing import TYPE_CHECKING, Any, cast

from mineworker import setting
from mineworker.exceptions import RequestError
from mineworker.network.downloader._common import (
    ProxyClientCache,
    check_content_type,
    pick_proxy,
    read_capped,
    report_bad_proxy,
    resolve_impersonate,
    send_kwargs,
    shard_count,
    shard_index,
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
        # 按实际使用的代理缓存，而不是拿下载器的固定代理去比 ——
        # 后者在开代理池时永远不相等，于是每个请求都新建一个 session
        self._sessions = ProxyClientCache()
        # 每个代理一个 cookie jar，被该代理的所有分片共用 —— 见 `_jar_for`
        self._jars = ProxyClientCache()

    # ------------------------------------------------------------------
    def _make_session(
        self,
        proxy: str | None,
        verify: bool,
        cookies: dict[str, str] | CookieJar | None = None,
    ) -> CurlSession:
        kwargs: dict[str, Any] = {"verify": verify}
        if proxy:
            kwargs["proxy"] = proxy
        # `is not None` 而不是真值判断：空的 CookieJar 是假值，但必须传进去，
        # 否则 curl_cffi 会自己另建一个，分片之间就不共享了
        if cookies is not None:
            kwargs["cookies"] = cookies
        session: CurlSession = _requests().Session(**kwargs)
        return session

    def _session_for(self, request: Request) -> tuple[CurlSession, bool, str | None]:
        """返回 (session, 用完是否关闭, 本次使用的代理)。"""
        proxy = pick_proxy(request, self._proxy)
        verify = request.requests_kwargs.get("verify", self._verify)
        cookies = request.requests_kwargs.get("cookies")
        if self._use_session and not cookies and verify == self._verify:
            return self._session_for_proxy(proxy), False, proxy
        return self._make_session(proxy, verify, cookies), True, proxy

    def _jar_for(self, proxy: str | None, capacity: int) -> CookieJar:
        """这个代理的 cookie jar —— **所有分片共用同一个**。

        curl_cffi 和 httpx 一样，收到裸 `CookieJar` 时按引用使用
        （`Cookies.__init__` 的 else 分支），而 `CookieJar` 自带 `_cookies_lock`。
        所以分片不改变 cookie 语义。

        ⚠️ **jar 的寿命必须盖过 session 的**：容量要和 session 缓存一样大
        （第一版只有它的 1/片数，代理一多 jar 先被换出，同代理的两个分片就分家了），
        且每次取 session 都要摸一下 jar，不能只在新建时摸。
        """
        jar, _ = self._jars.get_or_create(proxy, CookieJar, capacity=capacity)
        return jar  # type: ignore[no-any-return]

    def _session_for_proxy(self, proxy: str | None) -> CurlSession:
        """取这个代理对应的 session。**一个代理可能有多片。**

        ⚠️ 分片在这里买到的**不是连接复用** —— curl 这条路径压根没有连接复用
        （见 `download` 里的说明）。买到的是「别让一个 Session 被太多线程共用」：
        实测 64 线程共用一个 242 QPS，按 16 线程分片后 589 QPS（2.4×），
        而每线程一个是 596 —— 分片已经追平，不必一线程一个。
        **拐点是 16 不是 32**（httpx 那边是 32）：curl 共用一个 Session 从 16 线程
        起就不再增长，所以有独立的 `CURL_SESSION_SHARD_THREADS`。
        curl_cffi 自己的 Session 文档也写着「建议每个线程一个 session」。
        """
        shards = shard_count(per_shard=setting.CURL_SESSION_SHARD_THREADS)
        key = proxy if shards <= 1 else f"{proxy}#{shard_index(shards)}"
        capacity = max(setting.SESSION_CACHE_SIZE, 1) * shards
        # **每次都摸一下 jar**，不只在新建 session 时 —— 见 `_jar_for` 的说明
        jar = self._jar_for(proxy, capacity)
        session, evicted_list = self._sessions.get_or_create(
            key,
            lambda: self._make_session(proxy, self._verify, cookies=jar),
            capacity=capacity,
        )
        for evicted in evicted_list:
            with contextlib.suppress(Exception):
                evicted.close()
        return session  # type: ignore[no-any-return]

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
            # stream=True：先拿响应头再决定读不读 body，和 httpx 那边同一个道理。
            #
            # ⚠️ **代价是这条路径没有连接复用**：curl_cffi 的
            # `Response._finalize_stream()` 收尾时执行 `self.curl.close()` ——
            # 关掉的是整个 Curl 句柄，句柄的连接缓存随之消失，不是把连接归还。
            # 实测（同线程 5 次串行、数保活 socket）：
            #     stream=False → 2 条保活    stream=True → 0 条
            # impersonate 开不开都一样，httpx 对照组是 2 条。
            # 所以 `use_session=True` 在 curl 这边**只带来 cookie 持久化**，
            # 不带来连接复用 —— 这一点先前的注释和文档都写反了。
            # 想拿回复用就得放弃 MAX_RESPONSE_SIZE 的边读边判，那是安全边界，不换。
            # 标成 Any：curl_cffi 的 iter_content / close 没有类型标注，
            # 在 strict 下会报 no-untyped-call。这里比逐行 type: ignore 干净
            resp: Any = session.request(
                cast("HttpMethod", request.method), request.url, stream=True, **kwargs
            )
            try:
                check_content_type(resp.headers, request.url)
                content = read_capped(
                    resp.iter_content(), request.url, resp.headers.get("content-length")
                )
            finally:
                resp.close()
        except _requests().RequestsError as exc:
            if proxy:
                report_bad_proxy(proxy)
            raise RequestError(f"下载失败 {request.method} {request.url}：{exc!r}") from exc
        finally:
            if should_close:
                session.close()
        return Response.from_curl_cffi(resp, request, content=content)

    def close(self) -> None:
        for session in self._sessions.drain():
            with contextlib.suppress(Exception):
                session.close()
