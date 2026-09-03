"""``Item`` / ``UpdateItem`` —— 结构化数据对象。

用法::

    item = Item(title="x", url="https://...")
    item.table_name = "news"          # 不设则由类名推导（NewsItem -> news）
    yield item

指定 ``__unique_key__`` 后，:pyattr:`fingerprint` 只用这些字段算指纹，配合去重实现
「重跑不重复入库」。``UpdateItem`` 额外用 ``__update_key__`` 作为 upsert 的匹配键。
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from mineworker.utils import tools

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _to_snake(name: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", name).lower()


class Item:
    #: 显式表名；不设则由类名推导（去掉结尾的 ``_item``）
    __table_name__: ClassVar[str | None] = None
    #: 参与指纹计算的字段；为空则用全部非空字段
    __unique_key__: ClassVar[list[str] | None] = None
    #: 类级管道（点号路径）；实例可用 ``item.pipelines = [...]`` 覆盖
    __pipelines__: ClassVar[list[str] | None] = None

    def __init__(self, **fields: Any) -> None:
        self._table_name: str | None = None
        self._pipelines: list[str] | None = None
        for key, value in fields.items():
            setattr(self, key, value)

    # ------------------------------------------------------------------
    @property
    def table_name(self) -> str:
        explicit = self._table_name or type(self).__table_name__
        if explicit:
            return explicit
        snake = _to_snake(type(self).__name__)
        return snake[:-5] if snake.endswith("_item") else snake

    @table_name.setter
    def table_name(self, value: str) -> None:
        self._table_name = value

    @property
    def unique_key(self) -> list[str] | None:
        return type(self).__unique_key__

    @property
    def pipelines(self) -> list[str] | None:
        return self._pipelines if self._pipelines is not None else type(self).__pipelines__

    @pipelines.setter
    def pipelines(self, value: list[str] | None) -> None:
        self._pipelines = value

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.__dict__.items()
            if not key.startswith("_") and not callable(value)
        }

    @property
    def fingerprint(self) -> str:
        data = self.to_dict()
        keys = self.unique_key or sorted(data)
        parts = [f"{k}={data[k]!r}" for k in sorted(keys) if data.get(k) not in (None, "")]
        if not parts:  # unique_key 字段全空：退回全字段
            parts = [f"{k}={v!r}" for k, v in sorted(data.items())]
        return tools.get_fingerprint(self.table_name, *parts)

    def pre_to_db(self) -> None:
        """保存前钩子。子类覆写做字段清洗 / 补全。"""

    # ------------------------------------------------------------------
    def __repr__(self) -> str:
        return f"<{type(self).__name__}({self.table_name}) {self.to_dict()!r}>"


class UpdateItem(Item):
    #: upsert 的匹配键；写入时按这些字段查找已有记录并更新
    __update_key__: ClassVar[list[str] | None] = None

    @property
    def update_key(self) -> list[str]:
        return type(self).__update_key__ or self.unique_key or []
