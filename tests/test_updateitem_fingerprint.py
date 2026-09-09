"""`UpdateItem` 的指纹不能按 `__unique_key__` 算，否则更新会被去重吃掉。

两个功能各自都写在文档里：
  `__unique_key__` —— 指纹只用这些字段，用来「重跑不重复入库」
  `UpdateItem`     —— 按 `__update_key__` 更新已有记录

各自都对。但 `UpdateItem` 的语义正是「同一条记录、**新的值**」，
指纹按 key 算的话第二次更新和第一次同指纹 —— 被去重直接吃掉。
而 `ITEM_FILTER_ENABLE` 默认就是开的。

实测（同一个 sku，价格 100 → 120 → 150）：
    只设 __update_key__（文档示例）   写出去 [100, 120, 150]
    同时设 __unique_key__             写出去 **[100]**

对 `UpdateItem` 而言，把 sku 声明成唯一键是再自然不过的事。
"""

from __future__ import annotations

from typing import Any

import pytest

from mineworker import Item, UpdateItem, setting
from mineworker.buffer.item_buffer import ItemBuffer
from mineworker.dedup import Dedup
from mineworker.pipelines.base import BasePipeline
from mineworker.utils.stats import Stats


class _Pipe(BasePipeline):
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        self.rows.extend(items)
        return True

    def update_items(self, table: str, items: list[dict[str, Any]], keys: list[str]) -> bool:
        self.rows.extend(items)
        return True


class _Priced(UpdateItem):
    __table_name__ = "price"
    __update_key__ = ["sku"]
    __unique_key__ = ["sku"]  # 用户很自然会这么写 —— sku 确实是唯一键


class _PlainKeyed(Item):
    __table_name__ = "book"
    __unique_key__ = ["url"]


@pytest.fixture(autouse=True)
def _on(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(setting, "ITEM_FILTER_ENABLE", True)
    monkeypatch.setattr(setting, "FAILED_ITEM_PATH", str(tmp_path / "failed_items.jsonl"))


def _write(items: list[Any]) -> list[dict[str, Any]]:
    pipe = _Pipe()
    buffer = ItemBuffer(Stats(), pipelines=["dummy"], dedup=Dedup(filter_type="lite"))
    buffer._pipeline_cache[("dummy",)] = [pipe]
    for item in items:
        buffer.put(item)
        buffer.flush()
    return pipe.rows


def _priced(price: int) -> _Priced:
    item = _Priced()
    item.sku, item.price = "A1", price
    return item


def test_changed_values_get_through() -> None:
    """同一个 sku、价格在变 —— 每一次变化都必须写出去。"""
    rows = _write([_priced(100), _priced(120), _priced(150)])
    assert [r["price"] for r in rows] == [100, 120, 150], (
        "后续的价格更新被去重吃掉了 —— 而 UpdateItem 的语义正是「新的值」"
    )


def test_identical_updates_are_still_deduped() -> None:
    """别用「UpdateItem 从此不去重」换「更新写得进去」——
    值没变的空转重复仍然要挡住。"""
    rows = _write([_priced(100), _priced(100), _priced(100)])
    assert [r["price"] for r in rows] == [100], f"空转的重复更新写了 {len(rows)} 次"


def test_update_key_still_falls_back_to_unique_key() -> None:
    """只改**指纹**：`update_key` 仍可回退到 `unique_key` ——
    那是「怎么写」，不是「要不要写」。"""

    class _OnlyUnique(UpdateItem):
        __table_name__ = "price"
        __unique_key__ = ["sku"]

    item = _OnlyUnique()
    item.sku = "A1"
    assert item.update_key == ["sku"]


def test_plain_item_is_unaffected() -> None:
    """普通 Item 的按键去重一点不能变 —— 那是「重跑不重复入库」的基础。"""
    first, second = _PlainKeyed(), _PlainKeyed()
    first.url, first.title = "http://x/1", "旧标题"
    second.url, second.title = "http://x/1", "新标题"
    assert first.fingerprint == second.fingerprint

    rows = _write([first, second])
    assert len(rows) == 1
