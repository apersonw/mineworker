"""Redis 连接管理 + 一次性锁。

按 URL 缓存 `redis.Redis` 实例（``decode_responses=True``）。分布式 Spider / 持久化去重
用到的所有 Redis 访问都从这里拿客户端。
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

import redis

from mineworker import setting

_lock = threading.Lock()
_clients: dict[str, Any] = {}


def get_redis(url: str | None = None) -> Any:
    url = url or setting.REDIS_URL
    client = _clients.get(url)
    if client is None:
        with _lock:
            client = _clients.get(url)
            if client is None:
                client = redis.Redis.from_url(url, decode_responses=True)
                _clients[url] = client
    return client


def close_redis() -> None:
    for client in _clients.values():
        with contextlib.suppress(Exception):
            client.close()
    _clients.clear()


def key(*parts: str) -> str:
    """拼一个带命名空间前缀的 Redis key。"""
    return ":".join((setting.REDIS_KEY_PREFIX, *parts))


def acquire_once(client: Any, name: str, ttl: int = 86400) -> bool:
    """尝试拿一个一次性锁（不阻塞、不主动释放，靠 TTL 过期）。

    多节点同时启动时用它保证 ``start_requests`` 只被执行一次。
    """
    return bool(client.set(name, "1", nx=True, ex=ttl))
