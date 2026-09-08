"""分布式启动竞态：节点不能在「还没人播种」时就判定抓完了。

实测现象（MineWorkerHub 上两个 worker 容器同秒启动）：

    实例 11  17:12:40 启动 → 63 请求 / 60 条
    实例 12  17:12:40 启动 → 17:12:41 退出，**0 个请求**

只有一个节点能拿到种子锁，另一个看到空队列，而默认
``DONE_CHECK_TIMES=3 × DONE_CHECK_INTERVAL=0.5s`` = 1.5 秒就判定结束 ——
播种节点那时还没把种子推进队列。心跳检测也挡不住：播种节点在那一刻的
``pending`` 同样是 0，它自己还没开始拉活。

后果：配 N 个节点，可能 N-1 个立刻退出，并行度归零，而且没有任何报错。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import fakeredis
import pytest

from mineworker import setting
from mineworker.core import redis_scheduler
from mineworker.core.base_parser import BaseParser
from mineworker.core.redis_scheduler import RedisScheduler
from mineworker.network.request import Request


class _Parser(BaseParser):
    def start_requests(self) -> Iterator[Request]:
        return iter(())

    def parse(self, request: Request, response: Any) -> None:
        return None


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch) -> Iterator[RedisScheduler]:
    client = fakeredis.FakeRedis(decode_responses=True)
    # 两处都要 patch：redis_scheduler 里是 `from ... import get_redis` 的早绑定名字，
    # 只改模块属性对它无效
    monkeypatch.setattr(redis_scheduler, "get_redis", lambda url=None: client)
    monkeypatch.setattr("mineworker.db.redisdb.get_redis", lambda url=None: client)
    monkeypatch.setattr(setting, "DEDUP_FILTER", "redis")
    yield RedisScheduler(parser=_Parser(), redis_key="RACE", keep_alive=False)
    client.flushall()


def test_fresh_node_does_not_declare_done_immediately(scheduler: RedisScheduler) -> None:
    """刚起来、队列还空、也没见过任何活 —— 此时不能说「抓完了」。

    这正是第二个节点撞上的局面：播种节点还在启动，队列暂时是空的。
    """
    assert scheduler._is_done() is False, (
        "节点在启动瞬间就判定抓完了 —— 多节点部署里 N-1 个节点会立刻退出"
    )


def test_node_exits_once_the_grace_window_passes(
    scheduler: RedisScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """宽限期过去、确实没活，就该正常退出 —— 不能变成永远不结束。"""
    monkeypatch.setattr(setting, "SPIDER_STARTUP_GRACE", 0.0)
    assert scheduler._is_done() is True


def test_grace_only_applies_before_the_node_has_seen_work(scheduler: RedisScheduler) -> None:
    """见过活之后就按原来的规则判 —— 宽限期只保护「从没拿到过任务」的启动窗口。"""
    scheduler.stats.incr("request_ok")
    assert scheduler._is_done() is True


def test_keep_alive_still_never_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_scheduler, "get_redis", lambda url=None: client)
    monkeypatch.setattr("mineworker.db.redisdb.get_redis", lambda url=None: client)
    monkeypatch.setattr(setting, "SPIDER_STARTUP_GRACE", 0.0)
    sch = RedisScheduler(parser=_Parser(), redis_key="RACE2", keep_alive=True)
    sch.stats.incr("request_ok")
    assert sch._is_done() is False
