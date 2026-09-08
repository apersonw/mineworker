"""对**真实**数据库跑 SQL 管道。

单元测试只能证明「SQL 字符串长这样」，证明不了「数据库认这条 SQL」。MysqlPipeline
在此之前从没跑过真库 —— 这个文件补上这个缺口，Postgres 与 MySQL 用同一组用例覆盖。

没配 MINEWORKER_TEST_*_URL 就整体 skip，本地默认不拖慢。
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

import pytest

from mineworker import UpdateItem, setting

pytestmark = pytest.mark.integration

TABLE = "mw_it_items"


# ---- 建表 -------------------------------------------------------------
def _setup_pg(db: Any) -> None:
    db.execute(f"DROP TABLE IF EXISTS {TABLE}")
    db.execute(
        f"""CREATE TABLE {TABLE} (
               url    varchar(255) PRIMARY KEY,
               title  varchar(255),
               score  integer
           )"""
    )


def _setup_mysql(db: Any) -> None:
    db.execute(f"DROP TABLE IF EXISTS `{TABLE}`")
    db.execute(
        f"""CREATE TABLE `{TABLE}` (
               `url`   varchar(255) NOT NULL PRIMARY KEY,
               `title` varchar(255),
               `score` int
           ) DEFAULT CHARSET=utf8mb4"""
    )


def _rows(db: Any, quoted: str) -> list[dict[str, Any]]:
    return db.query(f"SELECT * FROM {quoted} ORDER BY url")


@pytest.fixture
def pg(postgres_db: Any) -> Any:
    from mineworker.pipelines.postgres import PostgresPipeline

    _setup_pg(postgres_db)
    return PostgresPipeline(db=postgres_db), postgres_db, TABLE


@pytest.fixture
def my(mysql_db: Any) -> Any:
    from mineworker.pipelines.mysql import MysqlPipeline

    _setup_mysql(mysql_db)
    return MysqlPipeline(db=mysql_db), mysql_db, f"`{TABLE}`"


# ---- 两边共用的核心断言 ------------------------------------------------
def _check_save_and_read(pipe: Any, db: Any, quoted: str) -> None:
    items = [
        {"url": "https://a", "title": "标题一", "score": 1},
        {"url": "https://b", "title": "标题二", "score": 2},
    ]
    assert pipe.save_items(TABLE, items) is True
    rows = _rows(db, quoted)
    assert [r["url"] for r in rows] == ["https://a", "https://b"]
    assert [r["title"] for r in rows] == ["标题一", "标题二"]  # 中文往返
    assert [r["score"] for r in rows] == [1, 2]


def _check_none_value(pipe: Any, db: Any, quoted: str) -> None:
    assert pipe.save_items(TABLE, [{"url": "https://n", "title": None, "score": None}]) is True
    row = next(r for r in _rows(db, quoted) if r["url"] == "https://n")
    assert row["title"] is None and row["score"] is None


def _check_update_items(pipe: Any, db: Any, quoted: str) -> None:
    pipe.save_items(TABLE, [{"url": "https://u", "title": "旧", "score": 1}])
    assert pipe.update_items(TABLE, [{"url": "https://u", "title": "新", "score": 9}], ["url"])
    row = next(r for r in _rows(db, quoted) if r["url"] == "https://u")
    assert row["title"] == "新" and row["score"] == 9


# ---- PostgreSQL -------------------------------------------------------
def test_pg_save_and_read(pg: Any) -> None:
    _check_save_and_read(*pg)


def test_pg_none_value(pg: Any) -> None:
    _check_none_value(*pg)


def test_pg_update_items(pg: Any) -> None:
    _check_update_items(*pg)


def test_pg_on_conflict_nothing_keeps_first(pg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 DO NOTHING：主键重复时保留旧值，且整批不报错。"""
    pipe, db, quoted = pg
    monkeypatch.setattr(setting, "POSTGRES_ON_CONFLICT", "nothing")
    pipe.save_items(TABLE, [{"url": "https://x", "title": "第一次", "score": 1}])
    assert pipe.save_items(TABLE, [{"url": "https://x", "title": "第二次", "score": 2}]) is True
    row = next(r for r in _rows(db, quoted) if r["url"] == "https://x")
    assert row["title"] == "第一次"


def test_pg_on_conflict_update_overwrites(pg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    pipe, db, quoted = pg
    monkeypatch.setattr(setting, "POSTGRES_ON_CONFLICT", "update")
    monkeypatch.setattr(setting, "POSTGRES_CONFLICT_TARGET", ["url"])
    pipe.save_items(TABLE, [{"url": "https://y", "title": "第一次", "score": 1}])
    assert pipe.save_items(TABLE, [{"url": "https://y", "title": "第二次", "score": 2}]) is True
    row = next(r for r in _rows(db, quoted) if r["url"] == "https://y")
    assert row["title"] == "第二次" and row["score"] == 2


def test_pg_on_conflict_error_reports_failure(pg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """error 模式下主键冲突整批失败，返回 False（会被 dump 到 failed_items）。"""
    pipe, _db, _q = pg
    monkeypatch.setattr(setting, "POSTGRES_ON_CONFLICT", "error")
    pipe.save_items(TABLE, [{"url": "https://z", "title": "一", "score": 1}])
    assert pipe.save_items(TABLE, [{"url": "https://z", "title": "二", "score": 2}]) is False


# ---- MySQL ------------------------------------------------------------
def test_mysql_save_and_read(my: Any) -> None:
    _check_save_and_read(*my)


def test_mysql_none_value(my: Any) -> None:
    _check_none_value(*my)


def test_mysql_update_items(my: Any) -> None:
    _check_update_items(*my)


def test_mysql_on_duplicate_overwrites(my: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    pipe, db, quoted = my
    monkeypatch.setattr(setting, "MYSQL_UPDATE_ON_DUPLICATE", True)
    pipe.save_items(TABLE, [{"url": "https://x", "title": "第一次", "score": 1}])
    assert pipe.save_items(TABLE, [{"url": "https://x", "title": "第二次", "score": 2}]) is True
    row = next(r for r in _rows(db, quoted) if r["url"] == "https://x")
    assert row["title"] == "第二次" and row["score"] == 2


def test_mysql_without_on_duplicate_reports_failure(
    my: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipe, _db, _q = my
    monkeypatch.setattr(setting, "MYSQL_UPDATE_ON_DUPLICATE", False)
    pipe.save_items(TABLE, [{"url": "https://z", "title": "一", "score": 1}])
    assert pipe.save_items(TABLE, [{"url": "https://z", "title": "二", "score": 2}]) is False


# ---- 失败数据 dump 一圈回放之后 ---------------------------------------
class _RefusingPipeline:
    """写库失败 —— 这批会被 dump 到 failed_items。"""

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        return False

    def update_items(self, t: str, i: list[dict[str, Any]], k: list[str]) -> bool:
        return False

    def close(self) -> None:
        return None


class _PriceItem(UpdateItem):
    __table_name__ = TABLE
    __update_key__ = ["url"]


def test_failed_update_survives_the_dump_round_trip(
    pg: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写失败的 `UpdateItem` 经 dump → `retry --items` 之后，那次更新必须还在。

    判据是**库里那行的值**，不是 retry 的返回码 —— 假管道对什么都返回 True，
    正是这个缺陷能活下来的原因（`test_cli.py` 用的就是假管道）。

    修复前实测：dump 只记了 table + data，retry 于是无条件走 `save_items`，
    而默认 `on_conflict="nothing"` 让这条 INSERT 什么都不做**并返回成功**；
    retry 报告「成功 1，仍失败 0」、删掉文件，库里那行还是旧值。
    静默、永久、还报告成功。
    """
    from mineworker.buffer.item_buffer import ItemBuffer
    from mineworker.commands.retry import retry_items
    from mineworker.dedup import Dedup
    from mineworker.utils.stats import Stats

    pipe, db, quoted = pg
    dump = tmp_path / "failed.jsonl"
    monkeypatch.setattr(setting, "FAILED_ITEM_PATH", str(dump))
    monkeypatch.setattr(setting, "ITEM_FILTER_ENABLE", False)
    monkeypatch.setattr(setting, "POSTGRES_ON_CONFLICT", "nothing")
    monkeypatch.setattr(
        setting, "ITEM_PIPELINES", ["mineworker.pipelines.postgres.PostgresPipeline"]
    )
    # retry 会按 setting.POSTGRES_* 自己建连接，而夹具走的是 URL —— 对齐到同一个库
    parts = urlsplit(os.environ["MINEWORKER_TEST_POSTGRES_URL"])
    monkeypatch.setattr(setting, "POSTGRES_HOST", parts.hostname or "localhost")
    monkeypatch.setattr(setting, "POSTGRES_PORT", parts.port or 5432)
    monkeypatch.setattr(setting, "POSTGRES_USER", parts.username or "postgres")
    monkeypatch.setattr(setting, "POSTGRES_PASSWORD", parts.password or "")
    monkeypatch.setattr(setting, "POSTGRES_DB", parts.path.lstrip("/") or "mineworker")

    pipe.save_items(TABLE, [{"url": "https://dump", "title": "旧标题", "score": 1}])

    buf = ItemBuffer(
        Stats(),
        pipelines=[f"{__name__}._RefusingPipeline"],
        dedup=Dedup(filter_type="lite"),
    )
    item = _PriceItem()
    item.url, item.title, item.score = "https://dump", "新标题", 99
    buf.put(item)
    buf.flush()
    assert dump.exists(), "写库失败却没 dump，这个用例什么都没验到"

    ok, failed = retry_items()

    row = next(r for r in _rows(db, quoted) if r["url"] == "https://dump")
    assert (row["title"], row["score"]) == ("新标题", 99), (
        f"回放之后库里还是 {row['title']!r}/{row['score']} —— 那次更新被静默丢弃了，"
        f"而 retry 报告「成功 {ok}，仍失败 {failed}」并删掉了文件（最后一份副本）"
    )
