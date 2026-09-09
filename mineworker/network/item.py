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
    def _fingerprint_keys(self) -> list[str] | None:
        """算指纹时用哪些字段。普通 Item 就是 `unique_key`。"""
        return self.unique_key

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
        keys = self._fingerprint_keys or sorted(data)
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

    @property
    def _fingerprint_keys(self) -> list[str] | None:
        """`UpdateItem` 的指纹**永远按全字段算**，不看 `__unique_key__`。

        它的语义就是「同一条记录、**新的值**」。指纹按 key 算的话，
        第二次更新和第一次同指纹，会被去重直接吃掉 ——
        实测同一个 sku 的价格 100 → 120 → 150，只有 100 写了出去。

        按全字段算之后，内容**没变**的重复更新仍然会被挡住（同样的字段 → 同样的指纹），
        「丢掉空转的重复」和「放行真实的变化」两件事同时成立。

        注意只改**指纹**：`update_key` 仍然可以回退到 `unique_key` ——
        那是「怎么写」，不是「要不要写」。
        """
        return None
