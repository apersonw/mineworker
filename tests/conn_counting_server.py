"""数「接受了几条 TCP 连接」的靶子 —— 连接复用的**地面真相**。

为什么需要它：`test_session_reuse.py` 里每一条断言数的都是**对象**
（建了几个 client / 几个 session / 有没有 close），而 `test_end_to_end_session_reuse`
更是只断言 body 是不是 "pong" —— 把连接复用整个关掉，两者照样全绿。

代价是真实的：curl 下载器**从来没有复用过一条连接**（框架永远用 `stream=True`，
而 curl_cffi 收尾时会关掉整个 Curl 句柄），而这个事实瞒过了两个发布版本，
因为覆盖它的测试数的是「建了几个 Session」。

判据交给服务端：一条 TCP 连接进来，handler 就被实例化一次。
这是框架**外部**的事实，框架怎么想不影响它。
"""

from __future__ import annotations

import socketserver
import threading
from typing import Any

_BODY = b"<html><body>ok</body></html>"


class _Counter:
    def __init__(self) -> None:
        self.connections = 0
        self.requests = 0
        self._lock = threading.Lock()

    def conn(self) -> None:
        with self._lock:
            self.connections += 1

    def req(self) -> None:
        with self._lock:
            self.requests += 1


class _Handler(socketserver.StreamRequestHandler):
    counter: _Counter

    def setup(self) -> None:
        super().setup()
        # 每条 TCP 连接实例化一次 handler —— 这里加一，就是「开了几条连接」
        self.counter.conn()

    def handle(self) -> None:
        # keep-alive：一条连接上循环处理请求，直到对端关闭
        while True:
            line = self.rfile.readline()
            if not line or line in (b"\r\n", b"\n"):
                return
            while True:  # 吃掉请求头
                head = self.rfile.readline()
                if not head or head in (b"\r\n", b"\n"):
                    break
            self.counter.req()
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                b"Content-Length: " + str(len(_BODY)).encode() + b"\r\n"
                b"Connection: keep-alive\r\n\r\n" + _BODY
            )
            self.wfile.flush()


class ConnCountingServer:
    """``with ConnCountingServer() as srv:``；``srv.url`` 是根地址。"""

    def __init__(self) -> None:
        self.counter = _Counter()
        handler = type("_H", (_Handler,), {"counter": self.counter})
        self._srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self._srv.daemon_threads = True
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def connections(self) -> int:
        return self.counter.connections

    @property
    def requests(self) -> int:
        return self.counter.requests

    def __enter__(self) -> ConnCountingServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=5)
