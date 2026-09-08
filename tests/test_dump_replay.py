"""写库失败的数据，dump 一圈回放之后还是不是原来那条。

v4.14 把「dump 到 failed_items 也算落到了持久介质，可以销账」写成了保证。
这一组验的是那条退路本身。

实测（真 PG，默认 on_conflict="nothing"）：一条 UpdateItem 写库失败被 dump，
`mineworker retry --items` 报告「成功 1，仍失败 0」并删掉文件，
而库里那行还是旧值 —— 静默、永久、还报告成功。

链条：dump 只记 table + data → retry 无条件 save_items（INSERT 而非 UPSERT）
→ PG 的 DO NOTHING 让这条 INSERT 什么都不做**并返回成功** → retry 据此报喜。
每一环都「按自己的契约正确工作」。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mineworker import UpdateItem, setting
from mineworker.buffer.item_buffer import ItemBuffer
from mineworker.commands.retry import retry_items
from mineworker.dedup import Dedup
from mineworker.pipelines.base import BasePipeline
from mineworker.utils.stats import Stats


class RefusingPipeline(BasePipeline):
    """写库失败 —— 这批会被 dump。"""

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        return False

    def update_items(self, table: str, items: list[dict[str, Any]], keys: list[str]) -> bool:
        return False


class RecordingPipeline(BasePipeline):
    saved: list[tuple[str, list[dict[str, Any]]]] = []
    updated: list[tuple[str, list[dict[str, Any]], list[str]]] = []

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        RecordingPipeline.saved.append((table, items))
        return True

    def update_items(self, table: str, items: list[dict[str, Any]], keys: list[str]) -> bool:
        RecordingPipeline.updated.append((table, items, keys))
        return True


class InsertOnlyPipeline(BasePipeline):
    """没实现 update_items —— 基类会抛。retry 不能因此整个崩掉。"""

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        return True


_REFUSE = f"{__name__}.RefusingPipeline"
_RECORD = f"{__name__}.RecordingPipeline"


class _Price(UpdateItem):
    __table_name__ = "prices"
    __update_key__ = ["url"]


@pytest.fixture(autouse=True)
def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingPipeline.saved = []
    RecordingPipeline.updated = []
    monkeypatch.setattr(setting, "FAILED_ITEM_PATH", str(tmp_path / "failed.jsonl"))
    monkeypatch.setattr(setting, "ITEM_FILTER_ENABLE", False)


def _dump_one(item: Any) -> list[dict[str, Any]]:
    buf = ItemBuffer(Stats(), pipelines=[_REFUSE], dedup=Dedup(filter_type="lite"))
    buf.put(item)
    buf.flush()
    text = Path(setting.FAILED_ITEM_PATH).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_dump_keeps_update_keys() -> None:
    """不记 update_keys 的话，回放时这条就从 UPSERT 退化成 INSERT。"""
    item = _Price()
    item.url, item.price = "https://x", 99
    (record,) = _dump_one(item)
    assert record["update_keys"] == ["url"], (
        f"dump 丢了 update_keys：{record} —— 回放会变成普通 INSERT"
    )


def test_dump_keeps_pipeline_routing() -> None:
    """逐条指定了管道的数据，回放时不该被灌进当前全部 ITEM_PIPELINES。"""
    item = _Price()
    item.url, item.price = "https://x", 1
    item.pipelines = [_REFUSE]  # 路由到会失败的那个，才走得到 dump
    (record,) = _dump_one(item)
    assert record["pipelines"] == [_REFUSE]


def test_plain_item_dump_stays_the_old_shape() -> None:
    """普通 Item 没有 update_keys，dump 出来就该和以前一模一样。"""
    (record,) = _dump_one({"url": "https://x"})
    assert record == {"table": setting.ITEM_DEFAULT_TABLE, "data": {"url": "https://x"}}


def test_replay_routes_updates_to_update_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """带 update_keys 的记录必须走 update_items —— 这是整条链的关键一环。"""
    Path(setting.FAILED_ITEM_PATH).write_text(
        json.dumps({"table": "prices", "data": {"url": "u", "price": 9}, "update_keys": ["url"]})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setting, "ITEM_PIPELINES", [_RECORD])
    assert retry_items() == (1, 0)
    assert RecordingPipeline.updated == [("prices", [{"url": "u", "price": 9}], ["url"])]
    assert RecordingPipeline.saved == [], "UpdateItem 被当成普通插入回放了"


def test_replay_uses_the_recorded_pipelines(monkeypatch: pytest.MonkeyPatch) -> None:
    Path(setting.FAILED_ITEM_PATH).write_text(
        json.dumps({"table": "t", "data": {"k": 1}, "pipelines": [_RECORD]}) + "\n",
        encoding="utf-8",
    )
    # 当前配置是另一个管道；记录里指定了 RecordingPipeline，就该只走它
    monkeypatch.setattr(setting, "ITEM_PIPELINES", [_REFUSE])
    assert retry_items() == (1, 0)
    assert RecordingPipeline.saved == [("t", [{"k": 1}])]


def test_old_dump_files_still_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """老文件没有新字段，读的时候不能炸，也不能凭空当成 update。"""
    Path(setting.FAILED_ITEM_PATH).write_text(
        json.dumps({"table": "t", "data": {"k": 1}}) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(setting, "ITEM_PIPELINES", [_RECORD])
    assert retry_items() == (1, 0)
    assert RecordingPipeline.saved == [("t", [{"k": 1}])]
    assert RecordingPipeline.updated == []


def test_pipeline_without_update_items_does_not_kill_the_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """基类的 update_items 直接抛。这批该留在文件里，而不是让整个回放崩掉 ——
    那会连带丢掉本来能回放的其它记录。"""
    dump = Path(setting.FAILED_ITEM_PATH)
    dump.write_text(
        json.dumps({"table": "t", "data": {"k": 1}, "update_keys": ["k"]})
        + "\n"
        + json.dumps({"table": "t", "data": {"k": 2}})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setting, "ITEM_PIPELINES", [f"{__name__}.InsertOnlyPipeline"])
    ok, failed = retry_items()
    assert (ok, failed) == (1, 1), "普通那条本来能回放，不该被 update 那条连累"
    assert json.loads(dump.read_text(encoding="utf-8")) == {
        "table": "t",
        "data": {"k": 1},
        "update_keys": ["k"],
    }
