"""去重。

- :class:`LiteFilter`        进程内精确 set
- :class:`MemoryBloomFilter` 进程内布隆（省内存，极小概率误判）
- :class:`Dedup`             门面：按 ``DEDUP_FILTER`` 选实现，可选先 md5

Roadmap v2 会补 Redis 版布隆 / 时间窗过滤器，接口一致。
"""

from __future__ import annotations

from typing import Any, Protocol

from mineworker import setting
from mineworker.dedup.bloom_filter import MemoryBloomFilter
from mineworker.dedup.lite_filter import LiteFilter
from mineworker.exceptions import ConfigError
from mineworker.utils import tools

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


def _make_filter(filter_type: str, capacity: int, error_rate: float) -> Filter:
    if filter_type == "lite":
        return LiteFilter()
    if filter_type in {"memory", "bloom"}:
        return MemoryBloomFilter(capacity, error_rate)
    raise ConfigError(f"未知的 DEDUP_FILTER：{filter_type!r}（可选 memory / lite）")


class Dedup:
    """去重门面。``add(value)`` 返回 True=新，``get(value)`` / ``in`` 返回是否已存在。"""

    def __init__(
        self,
        *,
        filter_type: str | None = None,
        to_md5: bool | None = None,
        capacity: int | None = None,
        error_rate: float | None = None,
    ) -> None:
        self._to_md5 = setting.DEDUP_TO_MD5 if to_md5 is None else to_md5
        self._filter = _make_filter(
            filter_type or setting.DEDUP_FILTER,
            capacity or setting.DEDUP_INITIAL_CAPACITY,
            error_rate or setting.DEDUP_ERROR_RATE,
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


def get_request_filter() -> Dedup:
    """请求去重（fingerprint 已是 md5，不再二次 md5）。"""
    return Dedup(to_md5=False)


def get_item_filter() -> Dedup:
    """Item 去重（fingerprint 已是 md5，不再二次 md5）。"""
    return Dedup(to_md5=False)
