"""从一个 HTTP 接口拉取代理列表的代理池。

``PROXY_EXTRACT_API`` 返回的内容可以是每行一个代理，或一个 JSON 字符串数组。
拿到后轮流使用；某代理被 ``report_bad`` 或用满 ``PROXY_MAX_USE_TIMES`` 次后丢弃，
池空时（且距上次拉取超过 ``PROXY_MIN_INTERVAL``）重新拉取。
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque

import httpx

from mineworker import setting
from mineworker.network.proxy_pool.base import ProxyPool
from mineworker.utils import tools
from mineworker.utils.log import get_logger

log = get_logger("proxy_pool")


class ApiProxyPool(ProxyPool):
    def __init__(self, api: str | None = None) -> None:
        self._api = api or setting.PROXY_EXTRACT_API
        self._lock = threading.Lock()
        self._pool: deque[str] = deque()
        self._use_count: dict[str, int] = {}
        self._bad: set[str] = set()
        self._last_fetch = 0.0

    # ------------------------------------------------------------------
    def _fetch(self) -> None:
        if not self._api:
            return
        if time.monotonic() - self._last_fetch < setting.PROXY_MIN_INTERVAL:
            return
        self._last_fetch = time.monotonic()
        try:
            body = httpx.get(self._api, timeout=10).text.strip()
        except httpx.HTTPError as exc:
            log.error("拉取代理失败：{!r}", exc)
            return
        proxies = self._parse(body)
        for proxy in proxies:
            if proxy not in self._bad and proxy not in self._pool:
                self._pool.append(proxy)
        log.debug("代理池补充 {} 个，当前 {}", len(proxies), len(self._pool))

    @staticmethod
    def _parse(body: str) -> list[str]:
        if body.startswith("["):
            try:
                return [str(p) for p in tools.loads_json(body)]
            except ValueError:
                return []
        return [line.strip() for line in body.splitlines() if line.strip()]

    def _normalize(self, proxy: str) -> str:
        return proxy if "://" in proxy else f"http://{proxy}"

    # ------------------------------------------------------------------
    def get_proxy(self) -> str | None:
        with self._lock:
            if not self._pool:
                self._fetch()
            for _ in range(len(self._pool)):
                proxy = self._pool[0]
                self._pool.rotate(-1)  # 轮转到队尾
                if proxy in self._bad:
                    self._drop(proxy)
                    continue
                self._use_count[proxy] = self._use_count.get(proxy, 0) + 1
                if self._use_count[proxy] >= setting.PROXY_MAX_USE_TIMES:
                    self._drop(proxy)
                return self._normalize(proxy)
            return None

    def _drop(self, proxy: str) -> None:
        with contextlib.suppress(ValueError):
            self._pool.remove(proxy)

    def report_bad(self, proxy: str) -> None:
        with self._lock:
            raw = proxy.split("://", 1)[-1]
            self._bad.add(raw)
            self._bad.add(proxy)
            self._drop(raw)

    def close(self) -> None:
        with self._lock:
            self._pool.clear()
