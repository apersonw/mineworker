from __future__ import annotations

import csv
from pathlib import Path

import mongomock
import pytest

from mineworker.pipelines.base import BasePipeline
from mineworker.pipelines.console import ConsolePipeline
from mineworker.pipelines.csv import CsvPipeline
from mineworker.pipelines.mongo import MongoPipeline


def test_base_update_items_not_implemented_by_default() -> None:
    class OnlySave(BasePipeline):
        def save_items(self, table: str, items: list[dict[str, object]]) -> bool:
            return True

    with pytest.raises(NotImplementedError):
        OnlySave().update_items("t", [{}], ["k"])


def test_console_pipeline_returns_true() -> None:
    p = ConsolePipeline()
    assert p.save_items("t", [{"a": 1}]) is True
    assert p.update_items("t", [{"a": 1}], ["a"]) is True


def test_csv_pipeline_writes_rows(tmp_path: Path) -> None:
    p = CsvPipeline(output_dir=str(tmp_path))
    assert p.save_items("news", [{"title": "a", "url": "x"}, {"title": "b", "url": "y"}])
    assert p.save_items("news", [{"title": "c", "url": "z"}])
    p.close()

    rows = list(csv.DictReader((tmp_path / "news.csv").open(encoding="utf-8")))
    assert [r["title"] for r in rows] == ["a", "b", "c"]
    assert rows[0]["url"] == "x"


def test_csv_pipeline_appends_across_instances(tmp_path: Path) -> None:
    CsvPipeline(output_dir=str(tmp_path)).save_items("t", [{"k": "1"}])
    p2 = CsvPipeline(output_dir=str(tmp_path))
    p2.save_items("t", [{"k": "2"}])
    p2.close()
    text = (tmp_path / "t.csv").read_text(encoding="utf-8")
    assert text.count("\n") == 3  # header + 2 rows


def test_csv_pipeline_ignores_new_fields(tmp_path: Path) -> None:
    p = CsvPipeline(output_dir=str(tmp_path))
    p.save_items("t", [{"a": 1}])
    p.save_items("t", [{"a": 2, "b": 99}])
    p.close()
    rows = list(csv.DictReader((tmp_path / "t.csv").open(encoding="utf-8")))
    assert rows == [{"a": "1"}, {"a": "2"}]


def test_mongo_pipeline_insert_and_upsert() -> None:
    client = mongomock.MongoClient()
    p = MongoPipeline(db="test", client=client)

    assert p.save_items("news", [{"_id": 1, "t": "a"}, {"_id": 2, "t": "b"}])
    assert client["test"]["news"].count_documents({}) == 2

    assert p.update_items("news", [{"_id": 1, "t": "A"}], ["_id"])
    assert client["test"]["news"].find_one({"_id": 1})["t"] == "A"
    assert client["test"]["news"].count_documents({}) == 2

    assert p.update_items("news", [{"_id": 3, "t": "c"}], ["_id"])
    assert client["test"]["news"].count_documents({}) == 3
    p.close()


def test_mongo_pipeline_save_failure_returns_false() -> None:
    client = mongomock.MongoClient()
    p = MongoPipeline(db="test", client=client)
    p.save_items("t", [{"_id": 1}])
    # 重复 _id -> insert_many 抛错 -> save_items 返回 False
    assert p.save_items("t", [{"_id": 1}]) is False
