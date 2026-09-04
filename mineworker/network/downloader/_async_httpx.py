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
import threading
from typing import TYPE_CHECKING, Any

import httpx

from mineworker import setting
from mineworker.exceptions import RequestError
from mineworker.network.downloader._common import pick_proxy, report_bad_proxy, send_kwargs
from mineworker.network.downloader.base import Downloader
from mineworker.network.response import Response
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.network.request import Request

log = get_logger("downloader.async")

_CLOSE_TIMEOUT = 5.0


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
            "verify": self._verify,
            "limits": limits,
        }
        if setting.HTTPX_HTTP2:
            kwargs["http2"] = True
        self._client = httpx.AsyncClient(**kwargs)
        self._sem = asyncio.Semaphore(self._concurrency)

    # ------------------------------------------------------------------
    def download(self, request: Request) -> Response:
        return self._submit(self._download(request))  # type: ignore[no-any-return]

    async def _download(self, request: Request) -> Response:
        assert self._client is not None and self._sem is not None
        kwargs = send_kwargs(request, self._timeout)
        proxy = pick_proxy(request, None)
        verify = request.requests_kwargs.get("verify", self._verify)
        cookies = request.requests_kwargs.get("cookies")

        async with self._sem:
            try:
                if proxy is not None or cookies:
                    one_shot: dict[str, Any] = {"follow_redirects": True, "verify": verify}
                    if proxy is not None:
                        one_shot["proxy"] = proxy
                    if cookies:
                        one_shot["cookies"] = cookies
                    async with httpx.AsyncClient(**one_shot) as client:
                        resp = await client.request(request.method, request.url, **kwargs)
                else:
                    resp = await self._client.request(request.method, request.url, **kwargs)
            except httpx.HTTPError as exc:
                if proxy is not None:
                    report_bad_proxy(proxy)
                raise RequestError(f"下载失败 {request.method} {request.url}：{exc!r}") from exc
        return Response.from_httpx(resp, request)

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
        if self._client is not None:
            await self._client.aclose()
            self._client = None
