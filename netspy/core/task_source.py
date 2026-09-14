"""``TaskSpider`` 的任务源接口 + Redis list 实现。

任务是一段 JSON（一个 dict），生产者 ``push`` 进来，消费者 ``fetch`` 批量取走。
覆写 ``TaskSpider.fetch_tasks`` 可换成从 MySQL / Mongo 查。
"""

from __future__ import annotations

import abc
from typing import Any


class TaskSource(abc.ABC):
    @abc.abstractmethod
    def fetch(self, limit: int) -> list[str]:
        """取走至多 limit 个待处理任务（原始 JSON 字符串）。"""

    @abc.abstractmethod
    def push(self, *tasks: str) -> None:
        """追加任务。"""

    def size(self) -> int:
        return 0

    def close(self) -> None:  # noqa: B027 - 可选钩子
        """释放资源。"""


class RedisTaskSource(TaskSource):
    def __init__(self, redis_client: Any, key: str) -> None:
        self._r = redis_client
        self._key = key

    def fetch(self, limit: int) -> list[str]:
        result = self._r.lpop(self._key, max(1, limit))
        if result is None:
            return []
        return result if isinstance(result, list) else [result]

    def push(self, *tasks: str) -> None:
        if tasks:
            self._r.rpush(self._key, *tasks)

    def size(self) -> int:
        return int(self._r.llen(self._key))
