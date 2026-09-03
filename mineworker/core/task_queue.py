"""任务队列。

- :class:`MemoryTaskQueue` —— 单进程，``queue.PriorityQueue``
- :class:`RedisTaskQueue`  —— Redis zset（score=priority），多进程 / 多节点共享，
  支持断点续爬；接口与内存版一致

两者都提供 ``put`` / ``get`` / ``qsize`` / ``empty``；Redis 版额外提供 ``get_batch``。
"""

from __future__ import annotations

import itertools
import queue
from typing import TYPE_CHECKING, Any

from mineworker.utils import tools

if TYPE_CHECKING:
    from mineworker.network.request import Request


class MemoryTaskQueue:
    def __init__(self) -> None:
        self._q: queue.PriorityQueue[tuple[int, int, Request]] = queue.PriorityQueue()
        self._seq = itertools.count()

    def put(self, request: Request) -> None:
        self._q.put((request.priority, next(self._seq), request))

    def get(self, timeout: float | None = None) -> Request | None:
        try:
            return self._q.get(timeout=timeout)[2]
        except queue.Empty:
            return None

    def qsize(self) -> int:
        return self._q.qsize()

    def empty(self) -> bool:
        return self._q.empty()


class RedisTaskQueue:
    def __init__(self, name: str, redis_client: Any = None) -> None:
        self._r: Any = redis_client if redis_client is not None else _default_redis()
        self._key = f"{name}:z_requests"

    def put(self, request: Request) -> None:
        payload = tools.dumps_json(request.to_dict())
        self._r.zadd(self._key, {payload: request.priority})

    def get(self, timeout: float | None = None) -> Request | None:
        if timeout:
            popped = self._r.bzpopmin(self._key, timeout=timeout)
            member = popped[1] if popped else None
        else:
            rows = self._r.zpopmin(self._key, 1)
            member = rows[0][0] if rows else None
        return _decode(member)

    def get_batch(self, count: int) -> list[Request]:
        rows = self._r.zpopmin(self._key, max(1, count))
        return [req for member, _ in rows if (req := _decode(member)) is not None]

    def qsize(self) -> int:
        return int(self._r.zcard(self._key))

    def empty(self) -> bool:
        return self.qsize() == 0


def _decode(member: Any) -> Request | None:
    if member is None:
        return None
    from mineworker.network.request import Request

    return Request.from_dict(tools.loads_json(member))


def _default_redis() -> Any:
    from mineworker.db.redisdb import get_redis

    return get_redis()
