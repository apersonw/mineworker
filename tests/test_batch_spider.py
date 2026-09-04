from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import fakeredis
import pytest
from pytest_httpserver import HTTPServer

from mineworker import BatchSpider, Request, setting
from mineworker.core import redis_scheduler
from mineworker.core.batch_monitor import BatchMonitor
from mineworker.core.batch_store import DONE, FAILED, MemoryBatchStore, MysqlBatchStore
from mineworker.exceptions import SpiderError


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_scheduler, "get_redis", lambda url=None: client)
    monkeypatch.setattr("mineworker.db.redisdb.get_redis", lambda url=None: client)
    yield client
    client.flushall()


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "DONE_CHECK_INTERVAL": 0.05,
        "DONE_CHECK_TIMES": 2,
        "BUFFER_FLUSH_INTERVAL": 0.02,
        "HEARTBEAT_INTERVAL": 0.05,
        "HEARTBEAT_STALE": 5.0,
        "RANDOM_USER_AGENT": False,
        "SPIDER_THREAD_COUNT": 3,
        "TASK_POLL_INTERVAL": 0.03,
        "TASK_EXHAUST_POLLS": 2,
        "BATCH_MONITOR_INTERVAL": 0.03,
        "BATCH_LOST_TASK_STALE": 100.0,
    }.items():
        monkeypatch.setattr(setting, name, value)


# ====================================================================== MemoryBatchStore
def test_memory_store_claim_count_mark() -> None:
    store = MemoryBatchStore([{"id": 1}, {"id": 2}, {"id": 3}])
    assert store.count_tasks().todo == 3

    claimed = store.claim_tasks(2)
    assert [c["id"] for c in claimed] == [1, 2]
    assert store.count_tasks().doing == 2

    store.mark_task(1, DONE)
    store.mark_task(2, FAILED)
    counts = store.count_tasks()
    assert (counts.done, counts.failed, counts.todo, counts.settled) == (1, 1, 1, 2)


def test_memory_store_reset_all_and_lost() -> None:
    store = MemoryBatchStore([{"id": 1}, {"id": 2}])
    store.claim_tasks(2)
    assert store.reset_lost_tasks(100.0) == 0  # 刚认领不算丢
    assert store.reset_lost_tasks(0.0) == 2  # stale=0 → 全部算丢
    assert store.count_tasks().todo == 2

    store.claim_tasks(1)
    store.mark_task(1, DONE)
    store.reset_all_tasks()
    assert store.count_tasks().todo == 2


def test_memory_store_batch_record_lifecycle() -> None:
    store = MemoryBatchStore([{"id": 1}])
    assert store.latest_batch() is None

    batch = store.create_batch("d1", 10, 7.0, "day")
    assert store.latest_batch() is not None
    assert store.latest_batch().id == batch.id and not store.latest_batch().is_done

    store.update_batch_counts(batch.id, done=3, failed=1, total=10)
    assert store.latest_batch().done_count == 3

    store.finish_batch(batch.id)
    assert store.latest_batch().is_done
    assert store.batch_age_seconds(batch.id) >= 0


def test_memory_store_custom_fields() -> None:
    store = MemoryBatchStore([{"pk": 1, "state": 0}], id_field="pk", state_field="state")
    assert store.claim_tasks(5)[0]["pk"] == 1
    assert store.count_tasks().doing == 1
    store.mark_task(1, DONE)
    assert store.count_tasks().done == 1


# ====================================================================== BatchMonitor
def _drain_worker(
    redis: Any, key: str, store: MemoryBatchStore, *, until: int, retry_ids: set[int]
) -> None:
    """测试替身 worker：从 pending 取任务，标完成；retry_ids 里的 id 第一次遇到时假装崩溃。"""
    seen: dict[int, int] = {}
    deadline = time.time() + 5
    done = 0
    while done < until and time.time() < deadline:
        raw = redis.lpop(key, 20)
        for item in raw or []:
            task = json.loads(item)
            tid = task["id"]
            seen[tid] = seen.get(tid, 0) + 1
            if tid in retry_ids and seen[tid] == 1:
                continue  # 假装崩了，任务留在「处理中」
            store.mark_task(tid, DONE)
            done += 1
        time.sleep(0.01)


def test_monitor_run_once_completes_batch(fake_redis: Any) -> None:
    store = MemoryBatchStore([{"id": i} for i in range(4)])
    mon = BatchMonitor(
        store=store, redis=fake_redis, ns="mineworker:M", batch_interval=1, monitor_interval=0.01
    )
    worker = threading.Thread(
        target=_drain_worker,
        args=(fake_redis, "mineworker:M:batch_pending", store),
        kwargs={"until": 4, "retry_ids": set()},
    )
    worker.start()
    batch = mon.run_once()
    worker.join(timeout=5)

    assert batch.is_done and batch.done_count == 4
    assert store.count_tasks().done == 4
    assert fake_redis.get("mineworker:M:batch_done") == "1"


def test_monitor_reprocesses_lost_task(fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "BATCH_LOST_TASK_STALE", 0.02)
    store = MemoryBatchStore([{"id": 1}, {"id": 2}])
    mon = BatchMonitor(
        store=store,
        redis=fake_redis,
        ns="mineworker:L",
        batch_interval=1,
        monitor_interval=0.02,
        lost_stale=0.02,
    )
    worker = threading.Thread(
        target=_drain_worker,
        args=(fake_redis, "mineworker:L:batch_pending", store),
        kwargs={"until": 2, "retry_ids": {2}},  # 任务 2 第一次「崩溃」
    )
    worker.start()
    batch = mon.run_once()
    worker.join(timeout=5)

    assert batch.is_done
    assert store.count_tasks().done == 2  # 丢失的任务被重置后重跑成功


def test_monitor_empty_task_table_finishes_immediately(fake_redis: Any) -> None:
    mon = BatchMonitor(
        store=MemoryBatchStore([]), redis=fake_redis, ns="mineworker:Z", batch_interval=1
    )
    batch = mon.run_once()
    assert batch.is_done


def test_monitor_lock_blocks_second_master(fake_redis: Any) -> None:
    store = MemoryBatchStore([{"id": 1}])

    def make() -> BatchMonitor:
        return BatchMonitor(store=store, redis=fake_redis, ns="mineworker:LK", batch_interval=1)

    guard = make()._hold_lock()
    with pytest.raises(SpiderError, match="monitor 在运行"):
        make()._hold_lock()
    guard.__exit__()
    make()._hold_lock()  # 释放后可以再拿


def test_monitor_new_batch_waits_for_interval(fake_redis: Any) -> None:
    store = MemoryBatchStore([{"id": 1}])
    batch = store.create_batch("old", 1, interval=7.0, unit="day")
    store.finish_batch(batch.id)
    mon = BatchMonitor(
        store=store, redis=fake_redis, ns="mineworker:W", batch_interval=7, interval_unit="day"
    )
    # 上一批刚结束、远没到 7 天 → 不开新批次
    assert mon._ensure_batch(force=False) is None
    # 强制（run_once 语义）→ 开
    assert mon._ensure_batch(force=True) is not None


# ====================================================================== BatchSpider 端到端
class DemoBatch(BatchSpider):
    def __init__(self, base: str, **kw: Any) -> None:
        self._base = base
        self.items: list[Any] = []
        super().__init__(item_handler=self.items.extend, **kw)

    def task_requests(self, task: dict[str, Any]) -> Iterator[Request]:
        yield Request(f"{self._base}/item/{task['id']}", callback=self.parse)

    def parse(self, request: Request, response: Any, task: Any = None) -> Iterator[Any]:
        yield {"id": task["id"], "title": response.css("h1::text").get()}
        self.update_task(task["id"], ok=True)


def _serve(server: HTTPServer) -> str:
    for i in range(20):
        server.expect_request(f"/item/{i}").respond_with_data(
            f"<html><h1>item {i}</h1></html>", content_type="text/html"
        )
    server.expect_request("/item/boom").respond_with_data("nope", status=500)
    return server.url_for("/").rstrip("/")


def test_batch_end_to_end(httpserver: HTTPServer, fake_redis: Any) -> None:
    base = _serve(httpserver)
    store = MemoryBatchStore([{"id": 1}, {"id": 2}, {"id": 3}])
    spider = DemoBatch(base, batch_store=store, redis_key="E2E", keep_alive=False)

    monitor = threading.Thread(target=lambda: spider.start_monitor(once=True))
    monitor.start()
    spider.start()  # worker：任务耗尽即退出
    monitor.join(timeout=15)

    assert not monitor.is_alive()
    assert sorted(d["id"] for d in spider.items) == [1, 2, 3]
    assert store.count_tasks().done == 3
    latest = store.latest_batch()
    assert latest is not None and latest.is_done and latest.done_count == 3
    assert fake_redis.get("mineworker:E2E:batch_done") == "1"


def test_failed_request_marks_task_failed(httpserver: HTTPServer, fake_redis: Any) -> None:
    base = _serve(httpserver)
    store = MemoryBatchStore([{"id": "boom"}])

    class S(DemoBatch):
        __custom_setting__ = {"SPIDER_MAX_RETRY_TIMES": 1}

        def parse(self, request: Request, response: Any, task: Any = None) -> Iterator[Any]:
            if response.status_code != 200:
                raise ValueError("bad")
            yield from ()

    spider = S(base, batch_store=store, redis_key="FAIL", keep_alive=False)
    monitor = threading.Thread(target=lambda: spider.start_monitor(once=True))
    monitor.start()
    spider.start()
    monitor.join(timeout=15)

    assert not monitor.is_alive()
    assert store.count_tasks().failed == 1
    latest = store.latest_batch()
    assert latest is not None and latest.is_done and latest.fail_count == 1


def test_task_auto_injected_into_cb_kwargs(fake_redis: Any) -> None:
    spider = DemoBatch("http://x", batch_store=MemoryBatchStore([]), redis_key="CB")
    reqs = list(spider._task_requests_tagged({"id": 9, "extra": "x"}))
    assert reqs[0].cb_kwargs["task"] == {"id": 9, "extra": "x"}


# ====================================================================== MysqlBatchStore SQL 形状
class FakeDB:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.query_returns: dict[str, list[dict[str, Any]]] = {}
        self.insert_id = 7

    def query(self, sql: str, args: Any = None) -> list[dict[str, Any]]:
        self.calls.append(("query", sql, args))
        for sub, rows in self.query_returns.items():
            if sub in sql:
                return rows
        return []

    def execute(self, sql: str, args: Any = None) -> int:
        self.calls.append(("execute", sql, args))
        return 1

    def insert(self, sql: str, args: Any = None) -> int:
        self.calls.append(("insert", sql, args))
        return self.insert_id

    def close(self) -> None:
        self.calls.append(("close", "", None))


def test_mysql_store_claim_selects_then_updates_by_id() -> None:
    db = FakeDB()
    db.query_returns["SELECT * FROM `crawl_task`"] = [{"id": 5}, {"id": 6}]
    rows = MysqlBatchStore("crawl_task", db=db).claim_tasks(10)

    assert rows == [{"id": 5}, {"id": 6}]
    assert [c[0] for c in db.calls] == ["query", "execute"]
    assert "WHERE `batch_status`=0 LIMIT %s" in db.calls[0][1]
    assert "SET `batch_status`=2 WHERE `id` IN (%s, %s)" in db.calls[1][1]
    assert db.calls[1][2] == (5, 6)


def test_mysql_store_count_tasks_aggregates_by_state() -> None:
    db = FakeDB()
    db.query_returns["GROUP BY"] = [{"s": 0, "c": 3}, {"s": 1, "c": 5}, {"s": -1, "c": 1}]
    counts = MysqlBatchStore("t", db=db).count_tasks()
    assert (counts.total, counts.todo, counts.done, counts.failed) == (9, 3, 5, 1)


def test_mysql_store_create_batch_uses_insert_id() -> None:
    db = FakeDB()
    db.insert_id = 42
    record = MysqlBatchStore("t", db=db).create_batch("2026-09-04 10:00:00", 100, 7.0, "day")
    assert record.id == 42 and record.total_count == 100 and record.is_done is False
    assert db.calls[0][0] == "insert"


def test_mysql_store_custom_fields_in_sql() -> None:
    db = FakeDB()
    store = MysqlBatchStore("t", db=db, id_field="tid", state_field="st", time_field="mt")
    store.mark_task(9, DONE)
    assert db.calls[-1][1] == "UPDATE `t` SET `st`=%s WHERE `tid`=%s"
    assert db.calls[-1][2] == (DONE, 9)

    store.reset_lost_tasks(600)
    assert "WHERE `st`=2 AND `mt` < DATE_SUB(NOW(), INTERVAL %s SECOND)" in db.calls[-1][1]


def test_mysql_store_ensure_schema_creates_record_table() -> None:
    db = FakeDB()
    MysqlBatchStore("crawl_task", db=db).ensure_schema()
    assert "CREATE TABLE IF NOT EXISTS `crawl_task_batch_record`" in db.calls[0][1]


def test_mysql_store_reset_all_and_finish() -> None:
    db = FakeDB()
    store = MysqlBatchStore("t", db=db)
    store.reset_all_tasks()
    assert db.calls[-1][1] == "UPDATE `t` SET `batch_status`=0"
    store.finish_batch(3)
    assert "SET `is_done`=1" in db.calls[-1][1] and db.calls[-1][2] == (3,)
