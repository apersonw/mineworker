"""PostgresPipeline —— SQL 形状（不需要真数据库）。

真库行为在 tests/test_sql_integration.py 里验，那边才能证明「SQL 字符串数据库认不认」。
"""

from __future__ import annotations

from typing import Any

import pytest

from mineworker import setting
from mineworker.pipelines.postgres import PostgresPipeline


class FakeDB:
    """替身 PostgresDB：记录每次调用，可让某类操作抛错。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.closed = False
        self.raise_on: str | None = None

    def execute(self, sql: str, args: Any = None) -> int:
        self.calls.append(("execute", sql, args))
        if self.raise_on == "execute":
            raise RuntimeError("boom")
        return 1

    def executemany(self, sql: str, args_list: Any) -> int:
        rows = list(args_list)
        self.calls.append(("executemany", sql, rows))
        if self.raise_on == "executemany":
            raise RuntimeError("boom")
        return len(rows)

    def close(self) -> None:
        self.closed = True


ITEMS = [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]


# ---- save_items 的三种冲突模式 --------------------------------------
def test_on_conflict_nothing_is_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "POSTGRES_ON_CONFLICT", "nothing")
    db = FakeDB()
    assert PostgresPipeline(db=db).save_items("news", ITEMS)
    _, sql, args = db.calls[0]
    assert sql == 'INSERT INTO "news" ("id", "title") VALUES (%s, %s) ON CONFLICT DO NOTHING'
    assert args == [(1, "a"), (2, "b")]


def test_on_conflict_error_is_plain_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "POSTGRES_ON_CONFLICT", "error")
    db = FakeDB()
    PostgresPipeline(db=db).save_items("news", ITEMS)
    assert db.calls[0][1] == 'INSERT INTO "news" ("id", "title") VALUES (%s, %s)'


def test_on_conflict_update_uses_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "POSTGRES_ON_CONFLICT", "update")
    monkeypatch.setattr(setting, "POSTGRES_CONFLICT_TARGET", ["id"])
    db = FakeDB()
    PostgresPipeline(db=db).save_items("news", ITEMS)
    assert db.calls[0][1] == (
        'INSERT INTO "news" ("id", "title") VALUES (%s, %s) '
        'ON CONFLICT ("id") DO UPDATE SET "title"=EXCLUDED."title"'
    )


def test_conflict_target_excluded_from_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """冲突目标列不该出现在 SET 里 —— Postgres 会直接报错。"""
    monkeypatch.setattr(setting, "POSTGRES_ON_CONFLICT", "update")
    monkeypatch.setattr(setting, "POSTGRES_CONFLICT_TARGET", ["id", "title"])
    db = FakeDB()
    PostgresPipeline(db=db).save_items("news", ITEMS)
    assert db.calls[0][1].endswith('ON CONFLICT ("id", "title") DO NOTHING')


def test_update_mode_without_target_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """漏填冲突列时降级成 DO NOTHING，而不是让整批抛异常。"""
    monkeypatch.setattr(setting, "POSTGRES_ON_CONFLICT", "update")
    monkeypatch.setattr(setting, "POSTGRES_CONFLICT_TARGET", [])
    db = FakeDB()
    pipe = PostgresPipeline(db=db)
    assert pipe.save_items("news", ITEMS)
    assert db.calls[0][1].endswith("ON CONFLICT DO NOTHING")
    # 只告警一次，不刷屏
    pipe.save_items("news", ITEMS)
    assert pipe._warned_no_target is True


def test_identifier_quoting_escapes_quotes() -> None:
    db = FakeDB()
    PostgresPipeline(db=db).save_items('we"ird', [{"a": 1}])
    assert 'INSERT INTO "we""ird"' in db.calls[0][1]


# ---- update_items ----------------------------------------------------
def test_update_items_sql() -> None:
    db = FakeDB()
    assert PostgresPipeline(db=db).update_items(
        "news", [{"id": 1, "title": "new"}], update_keys=["id"]
    )
    kind, sql, args = db.calls[0]
    assert kind == "execute"
    assert sql == 'UPDATE "news" SET "title"=%s WHERE "id"=%s'
    assert args == ("new", 1)


def test_update_items_skips_row_with_only_keys() -> None:
    db = FakeDB()
    assert PostgresPipeline(db=db).update_items("news", [{"id": 1}], update_keys=["id"])
    assert db.calls == []


# ---- 失败与生命周期 --------------------------------------------------
def test_save_items_failure_returns_false() -> None:
    db = FakeDB()
    db.raise_on = "executemany"
    assert PostgresPipeline(db=db).save_items("news", ITEMS) is False


def test_update_items_failure_returns_false() -> None:
    db = FakeDB()
    db.raise_on = "execute"
    assert PostgresPipeline(db=db).update_items("news", ITEMS, ["id"]) is False


def test_empty_batch_is_noop() -> None:
    db = FakeDB()
    pipe = PostgresPipeline(db=db)
    assert pipe.save_items("news", []) and pipe.update_items("news", [], ["id"])
    assert db.calls == []


def test_close_delegates() -> None:
    db = FakeDB()
    PostgresPipeline(db=db).close()
    assert db.closed
