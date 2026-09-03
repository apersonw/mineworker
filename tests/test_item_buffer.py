from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mineworker import Item, UpdateItem, setting
from mineworker.buffer.item_buffer import ItemBuffer
from mineworker.dedup import Dedup
from mineworker.pipelines.base import BasePipeline
from mineworker.utils.stats import Stats


class RecordingPipeline(BasePipeline):
    saved: list[tuple[str, list[dict[str, Any]]]] = []
    updated: list[tuple[str, list[dict[str, Any]], list[str]]] = []
    fail_tables: set[str] = set()

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        if table in self.fail_tables:
            return False
        RecordingPipeline.saved.append((table, items))
        return True

    def update_items(self, table: str, items: list[dict[str, Any]], update_keys: list[str]) -> bool:
        RecordingPipeline.updated.append((table, items, update_keys))
        return True


_PIPE = f"{__name__}.RecordingPipeline"


@pytest.fixture(autouse=True)
def _reset_recording() -> None:
    RecordingPipeline.saved.clear()
    RecordingPipeline.updated.clear()
    RecordingPipeline.fail_tables.clear()


def _buffer(**kw: Any) -> ItemBuffer:
    kw.setdefault("pipelines", [_PIPE])
    kw.setdefault("dedup", Dedup(filter_type="lite"))
    return ItemBuffer(Stats(), **kw)


class NewsItem(Item):
    __unique_key__ = ["url"]


def test_dicts_go_to_default_table() -> None:
    buf = _buffer()
    buf.put({"a": 1})
    buf.flush()
    assert RecordingPipeline.saved == [(setting.ITEM_DEFAULT_TABLE, [{"a": 1}])]


def test_items_grouped_by_table() -> None:
    buf = _buffer()
    buf.put(NewsItem(url="u1", title="a"))
    buf.put(NewsItem(url="u2", title="b"))
    a = NewsItem(title="x")
    a.table_name = "other"
    buf.put(a)
    buf.flush()

    tables = {t for t, _ in RecordingPipeline.saved}
    assert tables == {"news", "other"}
    news_rows = next(rows for t, rows in RecordingPipeline.saved if t == "news")
    assert len(news_rows) == 2


def test_item_level_dedup_within_and_across_flushes() -> None:
    buf = _buffer()
    buf.put(NewsItem(url="u1", title="a"))
    buf.put(NewsItem(url="u1", title="a-dup-same-batch"))
    buf.flush()
    buf.put(NewsItem(url="u1", title="a-dup-later"))
    buf.flush()

    saved_rows = [r for _, rows in RecordingPipeline.saved for r in rows]
    assert len(saved_rows) == 1
    assert buf._stats.get("item") == 1
    assert buf._stats.get("item_dedup_dropped") == 2


def test_update_item_routed_to_update_items() -> None:
    class PriceItem(UpdateItem):
        __update_key__ = ["sku"]

    buf = _buffer()
    buf.put(PriceItem(sku="S1", price=5))
    buf.flush()
    assert RecordingPipeline.updated == [("price", [{"sku": "S1", "price": 5}], ["sku"])]
    assert not RecordingPipeline.saved


def test_failed_save_dumps_and_skips_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    RecordingPipeline.fail_tables.add("news")
    dedup = Dedup(filter_type="lite")
    buf = _buffer(dedup=dedup)

    buf.put(NewsItem(url="u1", title="a"))
    buf.flush()

    dump = tmp_path / "failed_items.jsonl"
    assert dump.exists()
    assert "u1" in dump.read_text(encoding="utf-8")
    assert buf._stats.get("item_failed") == 1
    # 写失败 -> 指纹没入库 -> 重试还能再来一次
    assert dedup.get(NewsItem(url="u1", title="a").fingerprint) is False


def test_per_item_pipeline_override() -> None:
    it = NewsItem(url="u1")
    it.pipelines = [_PIPE]  # item 级覆盖
    buf = _buffer(pipelines=["nonexistent.BrokenPipeline"])  # 全局管道故意无效
    buf.put(it)
    buf.flush()
    assert len(RecordingPipeline.saved) == 1  # 用了 item 覆盖，没碰无效的全局管道


def test_handler_bypasses_pipelines_and_dedup() -> None:
    got: list[Any] = []
    buf = ItemBuffer(Stats(), handler=got.extend)
    buf.put(NewsItem(url="u1"))
    buf.put(NewsItem(url="u1"))
    buf.flush()
    assert len(got) == 2
    assert not RecordingPipeline.saved


def test_item_filter_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "ITEM_FILTER_ENABLE", False)
    buf = _buffer()
    buf.put(NewsItem(url="u1", title="a"))
    buf.put(NewsItem(url="u1", title="a"))
    buf.flush()
    saved_rows = [r for _, rows in RecordingPipeline.saved for r in rows]
    assert len(saved_rows) == 2
