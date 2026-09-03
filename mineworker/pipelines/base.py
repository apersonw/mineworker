"""``BasePipeline`` —— 数据落库接口。"""

from __future__ import annotations

import abc
from typing import Any


class BasePipeline(abc.ABC):
    @abc.abstractmethod
    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        """批量写入。返回 False 时该批会被 dump 到 failed_items，且不写去重指纹。"""

    def update_items(self, table: str, items: list[dict[str, Any]], update_keys: list[str]) -> bool:
        """按 ``update_keys`` upsert。仅当爬虫用 UpdateItem 时才需要实现。"""
        raise NotImplementedError(f"{type(self).__name__} 不支持 UpdateItem")

    def close(self) -> None:  # noqa: B027 - 可选生命周期钩子，子类按需覆盖
        """爬虫结束时调用，释放连接 / 文件句柄。"""
