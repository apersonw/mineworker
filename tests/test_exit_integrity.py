"""退出时的数据完整性：最后一道防线不能有洞（v4.8）。

0.8.1（优雅停止）、0.10.2（请求缓冲不丢）最终都汇入同一条路径 ——
**退出时把没跑完的东西保住**。这里测那条路径本身。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mineworker import setting
from mineworker.core.base_parser import BaseParser
from mineworker.core.scheduler import AirScheduler
from mineworker.network.request import Request


class _Parser(BaseParser):
    def start_requests(self) -> Iterator[Request]:
        return iter(())

    def parse(self, request: Request, response: Any) -> None:
        return None


@pytest.fixture
def dump_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "failed_requests.jsonl"
    monkeypatch.setattr(setting, "FAILED_REQUEST_PATH", str(path))
    monkeypatch.setattr(setting, "DUMP_UNFINISHED_ON_EXIT", True)
    return path


def _dumped(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---- A：任何退出路径都要 dump ----------------------------------------
def test_dump_happens_even_when_not_interrupted(dump_path: Path) -> None:
    """非中断退出（比如 `SPIDER_MAX_RUNTIME` 超时）也必须 dump。

    原来卡着 `self._interrupted`，而超时那条路径**刻意不设**这个标志
    （它只服务于「再按一次 Ctrl-C 强制退出」）—— 于是超时停止时存货全丢。

    这也是 0.10.2 兜底说法的落点：Redis 永久故障时请求一直留在缓冲区、
    爬虫永远不算完成、最后由超时停止，恰好是不 dump 的那条路。
    """
    sch = AirScheduler(parser=_Parser())
    assert sch._interrupted is False
    for i in range(5):
        sch._request_buffer.put(Request(f"http://site/{i}"))

    sch._on_shutdown()

    urls = [d["url"] for d in _dumped(dump_path)]
    assert sorted(urls) == [f"http://site/{i}" for i in range(5)], "非中断退出时存货没有落盘"


def test_nothing_dumped_when_there_is_no_leftover(dump_path: Path) -> None:
    """正常跑完的爬虫没有存货，不该平白多出文件。"""
    AirScheduler(parser=_Parser())._on_shutdown()
    assert not dump_path.exists()


def test_switch_still_disables_dumping(dump_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "DUMP_UNFINISHED_ON_EXIT", False)
    sch = AirScheduler(parser=_Parser())
    sch._request_buffer.put(Request("http://site/x"))

    sch._on_shutdown()

    assert not dump_path.exists()


def test_dumped_requests_round_trip(dump_path: Path) -> None:
    """dump 出来的东西要能还原成等价的 Request —— 否则回放拿到的是残缺的请求。"""
    sch = AirScheduler(parser=_Parser())
    original = Request(
        "http://site/x",
        method="POST",
        callback="parse_detail",
        priority=3,
        cb_kwargs={"task": {"id": 7}},
    )
    sch._request_buffer.put(original)

    sch._on_shutdown()

    revived = Request.from_dict(_dumped(dump_path)[0])
    assert revived.url == original.url
    assert revived.method == "POST"
    assert revived.callback == "parse_detail"
    assert revived.priority == 3
    assert revived.cb_kwargs == {"task": {"id": 7}}


# ---- B：推回 Redis 失败要落盘 ----------------------------------------
def test_redis_pushback_failure_falls_back_to_disk(
    dump_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """推回 Redis 队列失败时，剩下的请求必须落盘。

    `RedisScheduler._on_shutdown` **恰恰是在「Redis 出问题」的场景下被调用的** ——
    推不回去还不落盘的话，这些请求既不在队列里、也不在缓冲区里，静默消失。
    和 0.10.2 修的 `flush` 是同一个形状，只是换了个地方。
    """
    import fakeredis

    from mineworker.core.redis_scheduler import RedisScheduler

    monkeypatch.setattr(setting, "REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(
        "mineworker.db.redisdb.get_redis",
        lambda *a, **kw: fakeredis.FakeRedis(decode_responses=True),
    )
    sch = RedisScheduler(parser=_Parser(), redis_key="EXIT")

    class Broken:
        """第 3 次 put 起就一直失败 —— Redis 在停机过程中彻底不可用。"""

        def __init__(self) -> None:
            self.n = 0
            self.got: list[str] = []

        def put(self, request: Request) -> None:
            self.n += 1
            if self.n >= 3:
                raise ConnectionError("Redis 不可用")
            self.got.append(request.url)

    broken = Broken()
    monkeypatch.setattr(sch, "_task_queue", broken)
    monkeypatch.setattr(sch, "_heartbeat", None)
    for i in range(6):
        sch._request_buffer.put(Request(f"http://site/{i}"))

    sch._on_shutdown()

    urls = [d["url"] for d in _dumped(dump_path)]
    assert set(broken.got) | set(urls) == {f"http://site/{i}" for i in range(6)}, (
        "有请求既没推回 Redis、也没落盘 —— 丢了"
    )
    assert urls, "推回失败之后一条都没落盘"


def test_pushback_clears_filter_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """推回队列的请求要清掉 `filter_repeat`，否则会被自己写下的指纹挡掉。"""
    import fakeredis

    from mineworker.core.redis_scheduler import RedisScheduler

    monkeypatch.setattr(
        "mineworker.db.redisdb.get_redis",
        lambda *a, **kw: fakeredis.FakeRedis(decode_responses=True),
    )
    sch = RedisScheduler(parser=_Parser(), redis_key="EXIT2")

    pushed: list[Request] = []
    monkeypatch.setattr(
        sch, "_task_queue", type("Q", (), {"put": lambda _s, r: pushed.append(r)})()
    )
    monkeypatch.setattr(sch, "_heartbeat", None)
    sch._request_buffer.put(Request("http://site/x"))

    sch._on_shutdown()

    assert pushed and pushed[0].filter_repeat is False
