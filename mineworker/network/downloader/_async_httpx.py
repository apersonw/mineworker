"""异步 httpx 下载器（``DOWNLOADER_ASYNC=True`` 时启用）—— async 内核评估的落地产出。

一个专属事件循环线程 + 一个共享 :class:`httpx.AsyncClient` 承载所有在途连接。对外仍是
同步的 :meth:`Downloader.download`：工作线程把协程提交到内部 loop 并阻塞等结果（和渲染池
一个套路）。相比「一个 OS 线程一个在途请求」，这里连接池 / keep-alive / HTTP/2 多路复用
被所有 worker 共享，FD 占用也更低。

真正的「少量线程驱动上千并发」还需要 worker 侧批量分发（见 docs/async-kernel.md），本模块
只做下载这一层，API 与线程模型都不变。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import TYPE_CHECKING, Any

import httpx

from mineworker import setting
from mineworker.exceptions import RequestError
from mineworker.network.downloader._common import (
    ProxyClientCache,
    apick_proxy,
    check_content_type,
    read_capped,
    report_bad_proxy,
    send_kwargs,
    ssl_context_for,
)
from mineworker.network.downloader.base import Downloader
from mineworker.network.response import Response
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.network.request import Request

log = get_logger("downloader.async")

_CLOSE_TIMEOUT = 5.0


async def _stream(
    client: httpx.AsyncClient, request: Request, kwargs: dict[str, Any]
) -> tuple[httpx.Response, bytes]:
    """与同步版同样的边界检查，只是要用 `aiter_bytes`。

    `read_capped` 收的是同步迭代器，异步这边先把分片收进列表再交给它 ——
    上限判断的逻辑只有一份，不重写第二遍（重写就会有一份先漂移）。
    """
    async with client.stream(request.method, request.url, **kwargs) as resp:
        check_content_type(resp.headers, request.url)
        limit = setting.MAX_RESPONSE_SIZE
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if limit > 0 and total > limit:
                break  # 提前跳出即断开连接，剩下的字节不会再传
        content = read_capped(iter(chunks), request.url, resp.headers.get("content-length"))
    return resp, content


class AsyncHttpxDownloader(Downloader):
    def __init__(
        self,
        *,
        concurrency: int | None = None,
        timeout: float | None = None,
        verify: bool = True,
    ) -> None:
        self._timeout = timeout
        self._verify = verify
        self._concurrency = int(concurrency or setting.DOWNLOADER_ASYNC_CONCURRENCY)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="async-downloader", daemon=True
        )
        self._thread.start()
        self._client: httpx.AsyncClient | None = None
        # 有代理时原来每个请求建一个一次性 client —— 一轮全新的 TLS 握手 +
        # 代理隧道。实测 5 个请求建 5 个。改成按代理缓存复用
        self._proxied = ProxyClientCache()
        self._sem: asyncio.Semaphore | None = None
        self._submit(self._setup())

    # ------------------------------------------------------------------
    def _submit(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _setup(self) -> None:
        limits = httpx.Limits(
            max_connections=self._concurrency,
            max_keepalive_connections=min(self._concurrency, 100),
        )
        kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "verify": ssl_context_for(self._verify),
            "limits": limits,
        }
        if setting.HTTPX_HTTP2:
            kwargs["http2"] = True
        self._client = httpx.AsyncClient(**kwargs)
        self._sem = asyncio.Semaphore(self._concurrency)

    # ------------------------------------------------------------------
    def download(self, request: Request) -> Response:
        return self._submit(self._download(request))  # type: ignore[no-any-return]

    async def _client_for_proxy(self, proxy: str, verify: Any) -> httpx.AsyncClient:
        """这个代理对应的连接池。换出的要 `await aclose()` —— 异步 client 的关法
        和同步不一样，在同步上下文里调 close() 会留下没关的连接。"""
        client = self._proxied.get(proxy)
        if client is not None:
            return client  # type: ignore[no-any-return]
        client = httpx.AsyncClient(
            follow_redirects=True, verify=ssl_context_for(verify), proxy=proxy
        )
        for evicted in self._proxied.put(proxy, client):
            with contextlib.suppress(Exception):
                await evicted.aclose()
        return client

    async def _download(self, request: Request) -> Response:
        assert self._client is not None and self._sem is not None
        kwargs = send_kwargs(request, self._timeout)
        proxy = await apick_proxy(request, None)
        verify = request.requests_kwargs.get("verify", self._verify)
        cookies = request.requests_kwargs.get("cookies")

        async with self._sem:
            try:
                if cookies:
                    # 带 cookies 是每请求的状态，不能共用连接池 —— 用完即弃
                    one_shot: dict[str, Any] = {
                        "follow_redirects": True,
                        "verify": ssl_context_for(verify),
                        "cookies": cookies,
                    }
                    if proxy is not None:
                        one_shot["proxy"] = proxy
                    async with httpx.AsyncClient(**one_shot) as client:
                        resp, content = await _stream(client, request, kwargs)
                elif proxy is not None:
                    client = await self._client_for_proxy(proxy, verify)
                    resp, content = await _stream(client, request, kwargs)
                else:
                    resp, content = await _stream(self._client, request, kwargs)
            except httpx.HTTPError as exc:
                if proxy is not None:
                    report_bad_proxy(proxy)
                raise RequestError(f"下载失败 {request.method} {request.url}：{exc!r}") from exc
        return Response.from_httpx(resp, request, content=content)

    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._loop.is_closed():
            return
        try:
            self._submit(self._aclose())
        except Exception:
            log.debug("async 下载器关闭异常", exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=_CLOSE_TIMEOUT)
        if not self._loop.is_closed():
            self._loop.close()

    async def _aclose(self) -> None:
        for client in self._proxied.drain():
            with contextlib.suppress(Exception):
                await client.aclose()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
