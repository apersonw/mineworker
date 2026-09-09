"""集成靶场：一个「像真站点」的靶子，判据全由它给出。

它记录每条路径被请求了几次 —— 这是框架**外部**的事实，
框架自己的 stats 说什么都不影响它。

站点里故意埋了这些东西，好让「全特性同开」的一次运行能同时验到多条规则：

- `/` 首页里有**重复链接**（`/item/0`、`/item/1` 各出现两次）→ 验请求去重
- `/private/secret` 被 `robots.txt` 禁止 → 验 ROBOTS_OBEY（该路径必须 0 次请求）
- `/boom/404` → 验 4xx 不重试（必须恰好 1 次）
- `/boom/500` → 验重试次数（必须是 1 + SPIDER_MAX_RETRY_TIMES 次）
- `/boom/429` **前两次返回 429、第三次放行** → 验重试真的重试了而不是直接放弃
- `/gz` 返回 gzip → 验解压

用法见 `tests/test_full_stack_integration.py`。
"""

from __future__ import annotations

import gzip
import socketserver
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler


class Site:
    def __init__(self, *, n_items=12, slow=0.0):
        self.hits: Counter[str] = Counter()
        self.n_items = n_items
        self.slow = slow
        self._lock = threading.Lock()
        self._429_seen: Counter[str] = Counter()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # 静音
                pass

            def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                with outer._lock:
                    outer.hits[path] += 1

                if path == "/robots.txt":
                    return self._send(200, b"User-agent: *\nDisallow: /private/\n", "text/plain")
                if path == "/":
                    links = "".join(f'<a href="/item/{i}">i{i}</a>' for i in range(outer.n_items))
                    # 故意放重复链接 + 一个 robots 禁止的 + 各种坏路径
                    links += '<a href="/item/0">dup</a><a href="/item/1">dup</a>'
                    links += '<a href="/private/secret">no</a>'
                    links += '<a href="/boom/500">e5</a><a href="/boom/404">e4</a>'
                    links += '<a href="/boom/429">e9</a><a href="/gz">gz</a>'
                    return self._send(200, f"<html><body>{links}</body></html>".encode())
                if path.startswith("/item/"):
                    if outer.slow:
                        time.sleep(outer.slow)
                    n = path.rsplit("/", 1)[-1]
                    return self._send(
                        200,
                        f'<html><body><h1 class="t">标题{n}</h1>'
                        f'<span class="p">{n}</span></body></html>'.encode(),
                    )
                if path == "/gz":
                    raw = (
                        b"<html><body><h1 class='t'>gz</h1><span class='p'>99</span></body></html>"
                    )
                    body = gzip.compress(raw)
                    return self._send(200, body, extra={"Content-Encoding": "gzip"})
                if path == "/private/secret":
                    return self._send(200, b"<html><body>SECRET</body></html>")
                if path == "/boom/500":
                    return self._send(500, b"boom")
                if path == "/boom/404":
                    return self._send(404, b"nope")
                if path == "/boom/429":
                    with outer._lock:
                        outer._429_seen[path] += 1
                        n = outer._429_seen[path]
                    if n <= 2:  # 前两次 429，第三次放行 —— 验重试真的重试了
                        return self._send(429, b"slow down", extra={"Retry-After": "0"})
                    return self._send(
                        200,
                        b"<html><body><h1 class='t'>ok429</h1>"
                        b"<span class='p'>429</span></body></html>",
                    )
                return self._send(404, b"nope")

        self._H = H

    def __enter__(self):
        self._srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), self._H)
        self._srv.daemon_threads = True
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *e):
        self._srv.shutdown()
        self._srv.server_close()
        self._t.join(timeout=5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"
