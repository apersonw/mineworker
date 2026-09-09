"""压测用的真代理 —— 一律用 tinyproxy。

**别自己写代理。** 上一程手写过三次，三次都坏：第一版不支持 keep-alive，
而这恰好抵消了当时要测的「连接复用」，等于把被测变量测没了。

选 tinyproxy 而不是 mitmproxy 也是量出来的（128 并发 · 50ms · 共用 client）：

    直连         535 QPS   峰值在途 128
    tinyproxy    517 QPS   峰值在途 128   ← 只吃 3%
    mitmdump     372 QPS   峰值在途  79   ← 它自己就是瓶颈

装：``brew install tinyproxy``。

用法::

    with proxy() as url:            # url 形如 http://127.0.0.1:54321
        ...

⚠️ **配置里不能写 `ConnectPort`**：不写才允许所有端口 CONNECT，一旦写了就只放行
443/563。https 靶子跑在随机高端口上，届时每个请求都会被代理拒掉。
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

TINYPROXY = os.environ.get("TINYPROXY_BIN", "/usr/local/opt/tinyproxy/bin/tinyproxy")

_CONF = """\
Port {port}
Listen 127.0.0.1
Timeout 600
MaxClients {maxclients}
Allow 127.0.0.1
DisableViaHeader Yes
LogLevel Critical
PidFile "{pid}"
LogFile "{log}"
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.5):
            return True
        time.sleep(0.05)
    return False


@contextlib.contextmanager
def proxy(port: int | None = None, maxclients: int = 300) -> Iterator[str]:
    """起一个 tinyproxy，yield 它的 URL；退出时进程一定被收掉。

    **每次跑压测都起一个新的**：代理自己的连接表也是状态，复用同一个实例会让
    先跑的那一臂把 TIME_WAIT 堆给后跑的那一臂。这个坑让我报过一次不存在的回归。
    """
    port = port or free_port()
    tmp = Path(tempfile.mkdtemp(prefix="proxylab-"))
    conf = tmp / "tinyproxy.conf"
    conf.write_text(
        _CONF.format(port=port, maxclients=maxclients, pid=tmp / "pid", log=tmp / "log")
    )
    proc = subprocess.Popen(
        [TINYPROXY, "-d", "-c", str(conf)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_port(port):
            raise RuntimeError(f"tinyproxy 起不来（port {port}）—— 装了吗：brew install tinyproxy")
        time.sleep(0.5)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


class StaticPool:
    """压测专用代理池：固定列表、轮转、**不拉黑**。

    框架自带的 `ApiProxyPool` 一遇失败就 `report_bad` 永久拉黑，而 `_fetch` 又
    因为它在 `_bad` 里不肯放回 —— 单代理池被一次瞬时错误打空后再也起不来，
    后面每个请求都等满 `PROXY_WAIT_TIMEOUT` 再失败。第一次跑 A/B 就是这么废的
    （健康的 tinyproxy 在 960 个请求里也会重置 3 条 keep-alive 连接）。

    那是池的策略问题，和「下载器复不复用连接」无关，会把要测的信号整个淹掉。
    ``setting.PROXY_POOL = "proxylab.StaticPool"``，再设 `StaticPool.urls`。
    """

    urls: list[str] = []  # noqa: RUF012 - 压测脚本，故意用类属性做全局配置
    _i = 0

    def get_proxy(self) -> str | None:
        if not self.urls:
            return None
        StaticPool._i += 1
        return self.urls[StaticPool._i % len(self.urls)]

    def report_bad(self, proxy: str) -> None:
        """故意不实现 —— 见类文档。"""

    def close(self) -> None:
        pass
