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
    effective_concurrency,
    pool_limits,
    read_capped,
    report_bad_proxy,
    send_kwargs,
    shard_index,
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


def loop_count(threads: int | None = None) -> int:
    """要开几个事件循环。

    「一个事件循环线程 + N 个线程阻塞提交」这个模式在 N 超过 ~24 时**坍塌**：
    实测 20 线程 342 QPS / 均在途 17.5，32 线程掉到 92 / 4.7，48 线程 60 / 3.1。

    **这与本框架的逻辑无关**：把框架整个拿掉、只留一个 loop + N 个线程反复
    `run_coroutine_threadsafe(client.get(url), loop).result()`，坍塌一模一样
    （32 线程 89 QPS / 在途 4.6）。所以在框架里改逻辑救不了，只能多开循环。

    按 16 线程一个循环分片后（裸模式实测）：32 线程 506 QPS（5.7×）、
    48 线程 349（5.5×）、64 线程 259（4.5×）。
    """
    per_loop = setting.ASYNC_THREADS_PER_LOOP
    if per_loop <= 0:
        return 1
    n = max(threads if threads is not None else effective_concurrency(), 1)
    return max(1, -(-n // per_loop))


class _Shard:
    """一个事件循环 + 它自己的 client / 信号量 / 代理连接池。

    **每片的东西不能跨片用**：`httpx.AsyncClient` 绑定在创建它的那个事件循环上，
    拿到别的循环里去用会挂在错误的 loop 上。
    """

    __slots__ = ("client", "loop", "proxied", "sem", "thread")

    def __init__(self, index: int, concurrency: int, verify: Any) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name=f"async-downloader-{index}", daemon=True
        )
        self.thread.start()
        self.client: httpx.AsyncClient | None = None
        self.sem: asyncio.Semaphore | None = None
        # 有代理时原来每个请求建一个一次性 client —— 一轮全新的 TLS 握手 +
        # 代理隧道。实测 5 个请求建 5 个。改成按代理缓存复用
        self.proxied = ProxyClientCache()
        self.submit(self._setup(concurrency, verify))

    def submit(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    async def _setup(self, concurrency: int, verify: Any) -> None:
        kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "verify": ssl_context_for(verify),
            # 原来这里写的是 min(concurrency, 100)，与 max_connections 不等 ——
            # 正是 v4.31 认定为「连接每轮用完就被关掉」的那个形状。
            # 实测均在途从没超过 18，这个上限**根本碰不到**，所以那不是活 bug；
            # 改成 pool_limits 只是让两处口径一致，不是性能修复。
            "limits": pool_limits(concurrency),
        }
        if setting.HTTPX_HTTP2:
            kwargs["http2"] = True
        self.client = httpx.AsyncClient(**kwargs)
        self.sem = asyncio.Semaphore(concurrency)


class AsyncHttpxDownloader(Downloader):
    def __init__(
        self,
        *,
        concurrency: int | None = None,
        timeout: float | None = None,
        verify: bool = True,
        loops: int | None = None,
    ) -> None:
        self._timeout = timeout
        self._verify = verify
        self._concurrency = int(concurrency or setting.DOWNLOADER_ASYNC_CONCURRENCY)
        n_loops = loops if loops is not None else loop_count()
        self._shards = [_Shard(i, self._concurrency, self._verify) for i in range(max(n_loops, 1))]

    # ------------------------------------------------------------------
    def _shard(self) -> _Shard:
        """本线程用哪一片 —— 与同步下载器的连接池分片共用同一套编号。"""
        return self._shards[shard_index(len(self._shards))]

    def _submit(self, coro: Any) -> Any:
        return self._shard().submit(coro)

    # ------------------------------------------------------------------
    def download(self, request: Request) -> Response:
        shard = self._shard()
        return shard.submit(self._download(request, shard))  # type: ignore[no-any-return]

    async def _client_for_proxy(self, shard: _Shard, proxy: str, verify: Any) -> httpx.AsyncClient:
        """这个代理对应的连接池。换出的要 `await aclose()` —— 异步 client 的关法
        和同步不一样，在同步上下文里调 close() 会留下没关的连接。

        **缓存是每片一份**：AsyncClient 绑定在创建它的事件循环上，
        跨片复用会把它挂到别的 loop 上去。
        """

        def _build() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                follow_redirects=True,
                verify=ssl_context_for(verify),
                proxy=proxy,
                limits=pool_limits(self._concurrency),
            )

        client, evicted_list = shard.proxied.get_or_create(proxy, _build)
        for evicted in evicted_list:
            with contextlib.suppress(Exception):
                await evicted.aclose()
        return client  # type: ignore[no-any-return]

    async def _download(self, request: Request, shard: _Shard) -> Response:
        assert shard.client is not None and shard.sem is not None
        kwargs = send_kwargs(request, self._timeout)
        proxy = await apick_proxy(request, None)
        verify = request.requests_kwargs.get("verify", self._verify)
        cookies = request.requests_kwargs.get("cookies")

        async with shard.sem:
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
                    client = await self._client_for_proxy(shard, proxy, verify)
                    resp, content = await _stream(client, request, kwargs)
                else:
                    resp, content = await _stream(shard.client, request, kwargs)
            except httpx.HTTPError as exc:
                if proxy is not None:
                    report_bad_proxy(proxy)
                raise RequestError(f"下载失败 {request.method} {request.url}：{exc!r}") from exc
        return Response.from_httpx(resp, request, content=content)

    # ------------------------------------------------------------------
    def close(self) -> None:
        """**每一片都要收**：漏掉一片就是漏掉一个事件循环线程和它全部的连接。"""
        for shard in self._shards:
            self._close_shard(shard)

    @staticmethod
    def _close_shard(shard: _Shard) -> None:
        if shard.loop.is_closed():
            return
        try:
            shard.submit(AsyncHttpxDownloader._aclose(shard))
        except Exception:
            log.debug("async 下载器关闭异常", exc_info=True)
        shard.loop.call_soon_threadsafe(shard.loop.stop)
        shard.thread.join(timeout=_CLOSE_TIMEOUT)
        if not shard.loop.is_closed():
            shard.loop.close()

    @staticmethod
    async def _aclose(shard: _Shard) -> None:
        for client in shard.proxied.drain():
            with contextlib.suppress(Exception):
                await client.aclose()
        if shard.client is not None:
            await shard.client.aclose()
            shard.client = None
