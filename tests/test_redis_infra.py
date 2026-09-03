from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import fakeredis
import pytest

from mineworker import Request, setting
from mineworker.core.task_queue import RedisTaskQueue
from mineworker.db import redisdb
from mineworker.dedup import Dedup, get_request_filter
from mineworker.dedup.redis_filter import RedisBloomFilter, RedisSetFilter


@pytest.fixture
def rds(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redisdb, "get_redis", lambda url=None: client)
    yield client
    client.flushall()


# ---------------------------------------------------------------- redisdb
def test_get_redis_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[str] = []

    def fake_from_url(url: str, **_: Any) -> Any:
        made.append(url)
        return fakeredis.FakeRedis(decode_responses=True)

    monkeypatch.setattr(redisdb.redis.Redis, "from_url", staticmethod(fake_from_url))
    redisdb.close_redis()
    a = redisdb.get_redis("redis://x/0")
    b = redisdb.get_redis("redis://x/0")
    assert a is b
    assert made == ["redis://x/0"]
    redisdb.close_redis()


def test_key_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "REDIS_KEY_PREFIX", "proj")
    assert redisdb.key("z_requests") == "proj:z_requests"


def test_acquire_once(rds: Any) -> None:
    assert redisdb.acquire_once(rds, "lock:seed") is True
    assert redisdb.acquire_once(rds, "lock:seed") is False


# ---------------------------------------------------------------- RedisTaskQueue
def test_redis_queue_priority_order(rds: Any) -> None:
    q = RedisTaskQueue("t")
    q.put(Request("https://a", priority=300))
    q.put(Request("https://b", priority=100))
    q.put(Request("https://c", priority=200))
    assert q.qsize() == 3
    assert [q.get().url for _ in range(3)] == ["https://b", "https://c", "https://a"]
    assert q.get() is None
    assert q.empty()


def test_redis_queue_roundtrips_request_fields(rds: Any) -> None:
    q = RedisTaskQueue("t")
    q.put(Request("https://x", "POST", callback="parse_x", render=True, cb_kwargs={"p": 2}))
    got = q.get()
    assert got is not None
    assert got.method == "POST"
    assert got.callback == "parse_x"
    assert got.render is True
    assert got.cb_kwargs == {"p": 2}


def test_redis_queue_get_batch(rds: Any) -> None:
    q = RedisTaskQueue("t")
    for i in range(5):
        q.put(Request(f"https://{i}", priority=i))
    batch = q.get_batch(3)
    assert [r.url for r in batch] == ["https://0", "https://1", "https://2"]
    assert q.qsize() == 2


def test_redis_queue_survives_new_instance(rds: Any) -> None:
    RedisTaskQueue("t").put(Request("https://persisted"))
    assert RedisTaskQueue("t").get().url == "https://persisted"  # 断点续爬


def test_redis_queue_blocking_get(rds: Any) -> None:
    q = RedisTaskQueue("t")
    q.put(Request("https://ready"))
    assert q.get(timeout=1).url == "https://ready"  # 有数据立即返回
    assert q.get(timeout=0.1) is None  # 空队列超时返回 None


# ---------------------------------------------------------------- redis dedup
def test_redis_set_filter(rds: Any) -> None:
    f = RedisSetFilter("ns")
    assert f.add("a") is True
    assert f.add("a") is False
    assert "a" in f
    assert "b" not in f
    assert len(f) == 1


def test_redis_bloom_filter(rds: Any) -> None:
    f = RedisBloomFilter("ns", capacity=10_000, error_rate=1e-4)
    assert f.add("x") is True
    assert f.add("x") is False
    assert "x" in f
    assert "y" not in f


def test_redis_bloom_no_false_negatives(rds: Any) -> None:
    f = RedisBloomFilter("ns", capacity=5_000, error_rate=1e-3)
    keys = [f"k{i}" for i in range(1_000)]
    for k in keys:
        f.add(k)
    assert all(k in f for k in keys)


def test_redis_dedup_shared_across_instances(rds: Any) -> None:
    RedisSetFilter("ns").add("dup")
    assert RedisSetFilter("ns").add("dup") is False


def test_dedup_facade_redis(rds: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "DEDUP_FILTER", "redis")
    d = Dedup(name="run1")
    assert d.add("hello") is True
    assert d.add("hello") is False
    assert Dedup(name="run1").get("hello") is True  # 跨实例共享
    assert Dedup(name="run2").get("hello") is False  # 命名空间隔离


def test_dedup_facade_redis_set(rds: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "DEDUP_FILTER", "redis-set")
    d = get_request_filter(name="reqs")
    fp = Request("https://x?a=1").fingerprint
    assert d.add(fp) is True
    assert get_request_filter(name="reqs").get(fp) is True


def test_dedup_unknown_still_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from mineworker.exceptions import ConfigError

    monkeypatch.setattr(setting, "DEDUP_FILTER", "bogus")
    with pytest.raises(ConfigError):
        Dedup()
