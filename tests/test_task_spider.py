from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import fakeredis
import pytest
from pytest_httpserver import HTTPServer

from mineworker import Request, TaskSpider, setting
from mineworker.core import redis_scheduler
from mineworker.core.redis_task_scheduler import _TaskPoller
from mineworker.core.task_source import RedisTaskSource


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
        "TASK_POLL_INTERVAL": 0.05,
        "TASK_EXHAUST_POLLS": 2,
    }.items():
        monkeypatch.setattr(setting, name, value)


class ItemSpider(TaskSpider):
    def __init__(self, base: str, **kw: Any) -> None:
        self._base = base
        self.items: list[Any] = []
        super().__init__(item_handler=self.items.extend, **kw)

    def task_requests(self, task: Any) -> Iterator[Request]:
        yield Request(
            f"{self._base}/item/{task['id']}",
            callback=self.parse,
            cb_kwargs={"task": task},
        )

    def parse(self, request: Request, response: Any, task: Any = None) -> Iterator[Any]:
        yield {"id": task["id"], "title": response.css("h1::text").get()}


def _serve(server: HTTPServer) -> str:
    for i in range(20):
        server.expect_request(f"/item/{i}").respond_with_data(
            f"<html><h1>item {i}</h1></html>", content_type="text/html"
        )
    return server.url_for("/").rstrip("/")


# ----------------------------------------------------------------------
def test_drains_tasks_then_exits(httpserver: HTTPServer, fake_redis: Any) -> None:
    base = _serve(httpserver)
    ItemSpider.push_tasks({"id": 1}, {"id": 2}, {"id": 3})

    spider = ItemSpider(base)
    spider.start()  # keep_alive 默认 False -> 任务耗尽即退出

    assert sorted(d["id"] for d in spider.items) == [1, 2, 3]
    assert {d["title"] for d in spider.items} == {"item 1", "item 2", "item 3"}


def test_push_and_fetch_roundtrip(fake_redis: Any) -> None:
    ItemSpider.push_tasks({"id": 7}, {"id": 8})
    key = "mineworker:ItemSpider:tasks"
    assert fake_redis.llen(key) == 2

    src = RedisTaskSource(fake_redis, key)
    assert set(src.fetch(10)) == {'{"id": 7}', '{"id": 8}'}
    assert src.fetch(10) == []
    assert src.size() == 0


def test_tasks_shared_across_nodes_no_dup(httpserver: HTTPServer, fake_redis: Any) -> None:
    base = _serve(httpserver)
    ItemSpider.push_tasks(*({"id": i} for i in range(8)), task_key="shared")

    a = ItemSpider(base, redis_key="shared")
    b = ItemSpider(base, redis_key="shared")
    ta, tb = threading.Thread(target=a.start), threading.Thread(target=b.start)
    ta.start()
    tb.start()
    ta.join(timeout=20)
    tb.join(timeout=20)

    assert not ta.is_alive() and not tb.is_alive()
    assert sorted(d["id"] for d in a.items + b.items) == list(range(8))


def test_keep_alive_runs_until_stopped(httpserver: HTTPServer, fake_redis: Any) -> None:
    base = _serve(httpserver)
    spider = ItemSpider(base, keep_alive=True)

    def feed_then_stop() -> None:
        time.sleep(0.3)
        spider.add_tasks({"id": 5})
        time.sleep(0.6)
        spider.stop()

    threading.Thread(target=feed_then_stop).start()
    spider.start()

    assert spider.items == [{"id": 5, "title": "item 5"}]


def test_poller_exhausted_toggles(httpserver: HTTPServer, fake_redis: Any) -> None:
    sched = ItemSpider(_serve(httpserver), redis_key="ex").scheduler
    poller = _TaskPoller(
        fetch=lambda _: [], make_requests=lambda _: [], request_buffer=sched._request_buffer
    )
    poller._poll()
    poller._poll()
    assert poller.exhausted is True

    poller._empty_polls = 0
    poller._fetch = lambda _: [{"id": 1}]
    poller._poll()
    assert poller.exhausted is False


def test_custom_fetch_tasks(httpserver: HTTPServer, fake_redis: Any) -> None:
    base = _serve(httpserver)

    class DbSpider(ItemSpider):
        _pool = [{"id": 10}, {"id": 11}]

        def fetch_tasks(self, limit: int) -> list[Any]:
            out, self._pool = self._pool[:limit], self._pool[limit:]
            return out

    spider = DbSpider(base, redis_key="db")
    spider.start()
    assert sorted(d["id"] for d in spider.items) == [10, 11]
