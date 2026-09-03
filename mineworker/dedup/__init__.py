"""去重。

- :class:`LiteFilter`         进程内精确 set
- :class:`MemoryBloomFilter`  进程内布隆（省内存，极小概率误判）
- Redis 布隆 / Redis 精确 set（跨进程共享，见 ``dedup.redis_filter``）
- :class:`Dedup`              门面：按 ``DEDUP_FILTER`` 选实现，可选先 md5
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from mineworker import setting
from mineworker.dedup.bloom_filter import MemoryBloomFilter
from mineworker.dedup.lite_filter import LiteFilter
from mineworker.exceptions import ConfigError
from mineworker.utils import tools

if TYPE_CHECKING:
    from redis import Redis

__all__ = [
    "Dedup",
    "Filter",
    "LiteFilter",
    "MemoryBloomFilter",
    "get_item_filter",
    "get_request_filter",
]


class Filter(Protocol):
    """去重过滤器接口：``add`` 返回 True 表示新指纹。"""

    def add(self, key: str) -> bool: ...

    def __contains__(self, key: str) -> bool: ...


def _make_filter(
    filter_type: str,
    capacity: int,
    error_rate: float,
    *,
    name: str,
    redis_client: Redis | None,
) -> Filter:
    if filter_type == "lite":
        return LiteFilter()
    if filter_type in {"memory", "bloom"}:
        return MemoryBloomFilter(capacity, error_rate)
    if filter_type in {"redis", "redis-bloom"}:
        from mineworker.dedup.redis_filter import RedisBloomFilter

        return RedisBloomFilter(name, redis_client, capacity=capacity, error_rate=error_rate)
    if filter_type == "redis-set":
        from mineworker.dedup.redis_filter import RedisSetFilter

        return RedisSetFilter(name, redis_client)
    raise ConfigError(
        f"未知的 DEDUP_FILTER：{filter_type!r}（可选 memory / lite / redis / redis-set）"
    )


class Dedup:
    """去重门面。``add(value)`` 返回 True=新，``get(value)`` / ``in`` 返回是否已存在。"""

    def __init__(
        self,
        *,
        filter_type: str | None = None,
        to_md5: bool | None = None,
        capacity: int | None = None,
        error_rate: float | None = None,
        name: str = "mineworker",
        redis_client: Redis | None = None,
    ) -> None:
        self._to_md5 = setting.DEDUP_TO_MD5 if to_md5 is None else to_md5
        self._filter = _make_filter(
            filter_type or setting.DEDUP_FILTER,
            capacity or setting.DEDUP_INITIAL_CAPACITY,
            error_rate or setting.DEDUP_ERROR_RATE,
            name=name,
            redis_client=redis_client,
        )

    def _key(self, value: Any) -> str:
        text = value if isinstance(value, str) else tools.dumps_json(value, sort_keys=True)
        return tools.md5(text) if self._to_md5 else text

    def add(self, value: Any) -> bool:
        return self._filter.add(self._key(value))

    def get(self, value: Any) -> bool:
        return self._key(value) in self._filter

    def __contains__(self, value: Any) -> bool:
        return self.get(value)


def get_request_filter(
    name: str = "mineworker:requests", redis_client: Redis | None = None
) -> Dedup:
    """请求去重（fingerprint 已是 md5，不再二次 md5）。"""
    return Dedup(to_md5=False, name=name, redis_client=redis_client)


def get_item_filter(name: str = "mineworker:items", redis_client: Redis | None = None) -> Dedup:
    """Item 去重（fingerprint 已是 md5，不再二次 md5）。"""
    return Dedup(to_md5=False, name=name, redis_client=redis_client)
