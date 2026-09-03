"""去重。

阶段 02 提供精确的 :class:`LiteFilter`；阶段 03 补充布隆过滤器、``Dedup`` 门面与
Item 级去重，并按配置 ``DEDUP_FILTER`` 选择实现。
"""

from __future__ import annotations

from typing import Protocol

from mineworker.dedup.lite_filter import LiteFilter

__all__ = ["Filter", "LiteFilter", "get_request_filter"]


class Filter(Protocol):
    """去重过滤器接口：``add`` 返回 True 表示新指纹。"""

    def add(self, key: str) -> bool: ...

    def __contains__(self, key: str) -> bool: ...


def get_request_filter() -> Filter:
    """按配置返回请求去重过滤器（阶段 02 恒为 LiteFilter）。"""
    return LiteFilter()
