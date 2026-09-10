"""基于 httpx 的同步下载器。"""

from __future__ import annotations

import contextlib
from http.cookiejar import CookieJar
from typing import TYPE_CHECKING, Any

import httpx

from mineworker import setting
from mineworker.exceptions import RequestError
from mineworker.network.downloader._common import (
    CLIENT_ONLY_KEYS,
    ProxyClientCache,
    check_content_type,
    pick_proxy,
    pool_limits,
    read_capped,
    report_bad_proxy,
    send_kwargs,
    shard_count,
    shard_index,
    ssl_context_for,
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
        # 按「实际使用的代理」缓存连接池。原来只有一个 `self._client`，
        # 复用条件写的是 `proxy == self._proxy` —— 拿**配置**（下载器的固定代理）
        # 去比**状态**（这次实际用的代理），开代理池时两者永远不等，
        # 于是每个请求都新建一次 client。实测 5 个请求建了 5 个。
        #
        # ⚠️ **收益不是「连接复用」**（早先的注释和文档是这么写的，错了）。
        # 同一套 A/B 在两种代理下几乎一样：squid（全程复用）8/16/32 线程
        # 1.23× / 1.87× / 2.22×，tinyproxy（端到端都不保活）1.21× / 1.87× / 2.13×
        # —— 连接有没有真的被复用，对结果没有影响。
        # 真正省掉的是**每请求构造 / 销毁 client**：单线程下这笔账是 5.1ms/请求
        # （其中构造+关闭只占 0.78ms），而 16 线程下实测差距约 50ms/请求 ——
        # 并发把它放大了一个数量级，**放大的机制没有隔离出来**。
        #
        # 有界 LRU：代理池可能有上千个代理，不设上限就是把性能问题换成资源泄漏
        self._clients = ProxyClientCache()
        # 每个代理一个 cookie jar，被该代理的所有分片共用 —— 见 `_jar_for`
        self._jars = ProxyClientCache()

    # ------------------------------------------------------------------
    def _make_client(
        self,
        proxy: str | None,
        verify: bool,
        cookies: dict[str, str] | CookieJar | None = None,
    ) -> httpx.Client:
        kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "verify": ssl_context_for(verify),
            "limits": pool_limits(),
        }
        if setting.HTTPX_HTTP2:
            kwargs["http2"] = True
        if proxy:
            kwargs["proxy"] = proxy
        # `is not None` 而不是真值判断：空的 CookieJar 是假值，但它必须被传进去，
        # 否则 httpx 会自己建一个新的，分片间就不共享了
        if cookies is not None:
            kwargs["cookies"] = cookies
        return httpx.Client(**kwargs)

    def _client_for(self, request: Request) -> tuple[httpx.Client, bool, str | None]:
        """返回 (client, 用完是否关闭, 本次使用的代理)。"""
        proxy = pick_proxy(request, self._proxy)
        verify = request.requests_kwargs.get("verify", self._verify)
        cookies = request.requests_kwargs.get("cookies")
        # 带 cookies 或改了 verify 的请求不能共用连接池 —— 那是每请求的状态
        if self._use_session and not cookies and verify == self._verify:
            return self._session_client(proxy), False, proxy
        return self._make_client(proxy, verify, cookies), True, proxy

    def _jar_for(self, proxy: str | None, capacity: int) -> CookieJar:
        """这个代理的 cookie jar —— **所有分片共用同一个**。

        分片本来会把 cookie 切开（登录态落在 0 号片，下个请求走 1 号片就没了），
        而 `docs/settings.md` 对 `USE_SESSION` 的承诺是「连同 cookie jar 一起复用」。
        httpx 收到**裸 CookieJar** 时是按引用使用的（`Cookies.__init__` 的 else 分支），
        而 `CookieJar` 自带 `_cookies_lock`，本身线程安全 —— 于是分片不改变语义。

        ⚠️ **jar 的寿命必须盖过 client 的**，两件事缺一不可：

        1. 容量要和 client 缓存**一样大**。第一版 jar 缓存用的是默认的
           `SESSION_CACHE_SIZE`，而 client 缓存是它的**片数倍** ——
           代理一多，jar 先被换出、client 还活着，之后为同一代理新建分片就会
           拿到一个全新的 jar，两个分片的 cookie 从此分家。
        2. **每次取 client 都要摸一下 jar**，不能只在新建 client 时摸。
           否则一个 client 很热但从不重建的代理，它的 jar 在 LRU 里是冷的，
           照样会被换出。
        """
        jar, _ = self._jars.get_or_create(proxy, CookieJar, capacity=capacity)
        return jar  # type: ignore[no-any-return]

    def _session_client(self, proxy: str | None) -> httpx.Client:
        """取这个代理对应的连接池，没有就建一个。超出上限时关掉最久没用的。

        **一个代理可能有多片**：一个 client 被太多线程共用时，连接池自己成为
        争用点，吞吐到 ~32 线程见顶后掉头向下（见 `shard_count`）。
        """
        shards = shard_count()
        key = proxy if shards <= 1 else f"{proxy}#{shard_index(shards)}"
        # 一个代理占 K 个槽位，上限跟着放大，
        # 否则 SESSION_CACHE_SIZE 会从「缓存几个代理」变成「几个分片」
        capacity = max(setting.SESSION_CACHE_SIZE, 1) * shards
        # **每次都摸一下 jar**，不只在新建 client 时 —— 见 `_jar_for` 的说明
        jar = self._jar_for(proxy, capacity)
        client, evicted_list = self._clients.get_or_create(
            key,
            lambda: self._make_client(proxy, self._verify, cookies=jar),
            capacity=capacity,
        )
        for evicted in evicted_list:
            # 换出时必须关掉，否则连接和 fd 就泄漏了
            with contextlib.suppress(Exception):
                evicted.close()
        return client  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    def download(self, request: Request) -> Response:
        client, should_close, proxy = self._client_for(request)
        try:
            # 流式：先拿到响应头，再决定要不要读 body、读多少。这是唯一一个
            # 能在「2GB 已经进了内存」之前叫停的窗口 —— client.request() 返回时
            # body 已经读完了，那时再判断大小毫无意义
            with client.stream(
                request.method, request.url, **send_kwargs(request, self._timeout)
            ) as resp:
                check_content_type(resp.headers, request.url)
                content = read_capped(
                    resp.iter_bytes(), request.url, resp.headers.get("content-length")
                )
        except httpx.HTTPError as exc:
            if proxy:
                report_bad_proxy(proxy)
            raise RequestError(f"下载失败 {request.method} {request.url}：{exc!r}") from exc
        finally:
            if should_close:
                client.close()
        return Response.from_httpx(resp, request, content=content)

    def close(self) -> None:
        for client in self._clients.drain():
            with contextlib.suppress(Exception):
                client.close()
