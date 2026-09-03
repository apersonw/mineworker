"""代理池（阶段 06）：ProxyPool 接口 + ApiProxyPool。

``get_proxy_pool()`` 按 ``PROXY_ENABLE`` / ``PROXY_POOL`` 返回单例，供下载器使用。
"""

from __future__ import annotations

import threading

from mineworker import setting
from mineworker.network.proxy_pool.base import ProxyPool
from mineworker.utils import tools

__all__ = ["ProxyPool", "close_proxy_pool", "get_proxy_pool"]

_state: dict[str, ProxyPool | None] = {"pool": None}
_lock = threading.Lock()


def get_proxy_pool() -> ProxyPool | None:
    if not setting.PROXY_ENABLE:
        return None
    if _state["pool"] is None:
        with _lock:
            if _state["pool"] is None:
                _state["pool"] = tools.load_object(setting.PROXY_POOL)()
    return _state["pool"]


def close_proxy_pool() -> None:
    pool = _state["pool"]
    if pool is not None:
        pool.close()
        _state["pool"] = None
