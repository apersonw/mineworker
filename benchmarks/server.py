"""压测靶子：一个自身不会成为瓶颈的 asyncio HTTP 服务。

关键职责是**记录服务端观察到的并发峰值** —— 这是整个 benchmark 里最重要的一个数：
它直接回答「框架的在途连接数是不是真的卡在 SPIDER_THREAD_COUNT 上」，
也正是 docs/async-kernel.md 要求的那个证据。

用 asyncio 而不是线程池，是为了让靶子的并发能力远高于被测对象；否则测出来的
是靶子的极限，不是框架的。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Stats:
    """服务端视角的观测量。"""

    inflight: int = 0
    max_inflight: int = 0
    total: int = 0
    first_at: float | None = None
    last_at: float | None = None
    #: ∫inflight dt —— 用来算**时间加权平均在途**。峰值会被一瞬间的尖峰骗到，
    #: 平均才是解释吞吐的那个数（Little's Law: QPS = 平均在途 ÷ 延迟）
    _area: float = 0.0
    _changed_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _accrue(self, now: float) -> None:
        if self._changed_at is not None:
            self._area += self.inflight * (now - self._changed_at)
        self._changed_at = now

    def enter(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._accrue(now)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            if self.first_at is None:
                self.first_at = now

    def leave(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._accrue(now)
            self.inflight -= 1
            self.total += 1
            self.last_at = now

    def reset(self) -> None:
        with self._lock:
            self.inflight = 0
            self.max_inflight = 0
            self.total = 0
            self.first_at = None
            self.last_at = None
            self._area = 0.0
            self._changed_at = None

    @property
    def elapsed(self) -> float:
        """首个请求到达 → 末个请求完成。比外部计时更能反映真实服务时间。"""
        if self.first_at is None or self.last_at is None:
            return 0.0
        return max(self.last_at - self.first_at, 1e-9)

    @property
    def qps(self) -> float:
        return self.total / self.elapsed if self.total else 0.0

    @property
    def avg_inflight(self) -> float:
        """时间加权平均在途。**这是解释吞吐的那个数**，峰值不是。"""
        return self._area / self.elapsed if self.total else 0.0


#: 真实体量的 HTML：让 lxml 解析有实际成本，而不是空转
def make_body(n_items: int) -> bytes:
    rows = "".join(
        f'<li class="item" data-id="{i}">'
        f'<a href="/detail/{i}">条目 {i}</a>'
        f'<span class="price">{i * 3 % 997}</span></li>'
        for i in range(n_items)
    )
    return (
        f"<!doctype html><html><head><title>bench</title></head>"
        f'<body><h1>列表页</h1><ul id="list">{rows}</ul></body></html>'
    ).encode()


class BenchServer:
    """``async with BenchServer(...) as srv:`` 用法；``srv.url`` 是根地址。"""

    def __init__(self, *, latency: float = 0.05, n_items: int = 60, host: str = "127.0.0.1"):
        self.latency = latency
        self.body = make_body(n_items)
        self.host = host
        self.stats = Stats()
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    # -- HTTP（够用就行：只认请求行，固定返回一个 200）--------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                if not head:
                    break
                self.stats.enter()
                try:
                    if self.latency:
                        await asyncio.sleep(self.latency)
                    writer.write(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/html; charset=utf-8\r\n"
                        b"Content-Length: " + str(len(self.body)).encode() + b"\r\n"
                        b"Connection: keep-alive\r\n\r\n" + self.body
                    )
                    await writer.drain()
                finally:
                    self.stats.leave()
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    # -- 生命周期：跑在自己的线程 + 事件循环里 ---------------------------
    def _run(self) -> None:
        async def main() -> None:
            self._server = await asyncio.start_server(self._handle, self.host, 0)
            self.port = self._server.sockets[0].getsockname()[1]
            self._loop = asyncio.get_running_loop()
            self._ready.set()
            async with self._server:
                await self._server.serve_forever()

        with contextlib.suppress(asyncio.CancelledError):
            asyncio.run(main())

    def start(self) -> BenchServer:
        self._thread = threading.Thread(target=self._run, daemon=True, name="bench-server")
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("靶子启动超时")
        return self

    def stop(self) -> None:
        if self._loop is not None and self._server is not None:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> BenchServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


# ----------------------------------------------------------------------
_RAW_REQ = b"GET / HTTP/1.1\r\nHost: bench\r\nConnection: keep-alive\r\n\r\n"


async def _raw_hammer(host: str, port: int, n_conn: int, per_conn: int) -> None:
    """裸 asyncio 客户端。

    自检**必须**用裸客户端而不是 httpx：这一步要回答的是「靶子扛不扛得住」，
    用 httpx 会把客户端自己的连接池上限混进来，测出来的是两者的较小值。
    （实测过：同一个靶子，httpx 客户端只能压到 ~48 并发，裸 asyncio 能到 512。）
    """

    async def one_conn() -> None:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            for _ in range(per_conn):
                writer.write(_RAW_REQ)
                await writer.drain()
                head = await reader.readuntil(b"\r\n\r\n")
                length = int(
                    next(h for h in head.split(b"\r\n") if b"Content-Length" in h).split(b": ")[1]
                )
                await reader.readexactly(length)
        finally:
            writer.close()

    await asyncio.gather(*(one_conn() for _ in range(n_conn)))


def _selfcheck(concurrency: int, per_conn: int, latency: float) -> None:
    """靶子自证清白：确认它自己能扛到远高于被测并发。

    如果这一步的峰值上不去，后面测的就是靶子的极限而不是框架的。
    """
    with BenchServer(latency=latency) as srv:
        t0 = time.monotonic()
        asyncio.run(_raw_hammer(srv.host, srv.port, concurrency, per_conn))
        wall = time.monotonic() - t0
        s = srv.stats
        total = concurrency * per_conn
        print(f"  目标并发   : {concurrency}")
        print(f"  实际峰值   : {s.max_inflight}")
        print(f"  完成请求   : {s.total} / {total}")
        print(f"  QPS        : {total / wall:,.0f}")
        print(f"  wall       : {wall:.2f}s")
        ok = s.max_inflight >= concurrency * 0.9
        print(f"  结论       : {'OK —— 靶子扛得住' if ok else '⚠ 靶子没跑满，别拿它测更高并发'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="靶子自检：确认 server 本身不是瓶颈")
    ap.add_argument("--concurrency", type=int, default=512)
    ap.add_argument("--per-conn", type=int, default=10)
    ap.add_argument("--latency", type=float, default=0.05)
    args = ap.parse_args()
    _selfcheck(args.concurrency, args.per_conn, args.latency)
