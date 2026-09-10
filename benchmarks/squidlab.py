"""squid 前向代理 —— **要量连接复用就用这个，不要用 tinyproxy**。

`proxylab.py` 里的 tinyproxy 端到端都不保活：它把响应里的
`Connection: keep-alive` 整个剥掉，于是客户端每个请求都重新建连。
实测（裸 httpx，框架不参与，数到代理端口的 socket + 靶子接受的连接数）：

    直连         每次请求后 socket [1,1,1,1,1,1]   靶子接受 1 条 / 6 请求
    squid        每次请求后 socket [1,1,1,1,1,1]   靶子接受 1 条 / 6 请求
    tinyproxy    每次请求后 socket [0,0,0,0,0,0]   靶子接受 6 条 / 6 请求

⚠️ **别用 `client._transport._pool.connections` 判断有没有复用**：
带代理的 client 上它对 squid 也报 0，而 socket 计数显示连接确实被复用了。
判据要取库外部的事实 —— 数 socket、或让靶子数它接受了几条连接。

装：``brew install squid``。启动比 tinyproxy 慢不少（要初始化缓存结构）。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

SQUID = os.environ.get("SQUID_BIN", "/usr/local/opt/squid/sbin/squid")

_CONF = """\
http_port {port}
acl all src all
http_access allow all
# 不缓存：要量的是连接复用，缓存会把请求整个挡在代理上
cache deny all
cache_log {tmp}/cache.log
access_log none
pid_filename {tmp}/squid.pid
coredump_dir {tmp}
# 保活：这正是 tinyproxy 缺的东西
client_persistent_connections on
server_persistent_connections on
# 别让它把本地地址当成不可路由而拒绝
dns_v4_first on
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.5):
            return True
        time.sleep(0.1)
    return False


@contextlib.contextmanager
def squid(port: int | None = None) -> Iterator[str]:
    port = port or free_port()
    tmp = Path(tempfile.mkdtemp(prefix="squidlab-"))
    conf = tmp / "squid.conf"
    conf.write_text(_CONF.format(port=port, tmp=tmp))
    # squid 比 tinyproxy 启动慢得多（要初始化缓存结构），反复起停时更容易超时。
    # 起不来就重试一次并把日志带出来 —— 压测里一格失败会在表格上留个窟窿，
    # 而窟窿很容易被当成「这一格没意义」略过。
    proc = None
    for attempt in (1, 2):
        proc = subprocess.Popen(
            [SQUID, "-N", "-f", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if wait_port(port, timeout=60.0):
            break
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        if attempt == 2:
            log = (tmp / "cache.log").read_text()[-400:] if (tmp / "cache.log").exists() else ""
            raise RuntimeError(f"squid 起不来（port {port}）：{log}")
    try:
        time.sleep(0.5)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)
