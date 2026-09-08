"""租约续期（v4.13）—— 收拾 v4.12 自己带来的账。

v4.12 引入任务租约时，文档里写的「至少一次」说的是「节点卡住而非死了」。
**但更常见的情况没验到**：健康节点持有一批慢任务时，会自己把租约耗光。

租约是**从领走那刻起算的**，而 `COLLECTOR_TASK_COUNT` 默认一次领 100 个。
每页处理 10 秒的话，排在后面的任务在被碰到之前租约早就过期了 ——
节点全程健康却被判定「已死」，任务被别人抢去重抓一遍。

后果不是丢数据（去重挡得住重复入库），而是**重复抓取**：目标站挨双倍流量，
正好抵消这个框架主打的礼貌性。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import fakeredis
import pytest

from mineworker import setting
from mineworker.core.task_queue import RedisTaskQueue
from mineworker.network.request import Request

LEASE = 0.4


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setattr(setting, "SPIDER_TASK_LEASE", LEASE)
    yield fakeredis.FakeRedis(decode_responses=True)


def _filled(client: Any, name: str, n: int = 5) -> RedisTaskQueue:
    queue = RedisTaskQueue(name, client)
    for i in range(n):
        queue.put(Request(f"http://site/{i}"))
    return queue


# ---- 核心：两条必须同时成立 ------------------------------------------
def test_renewing_node_keeps_its_tasks(client: Any) -> None:
    """一直在续期的节点，任务一个都不该被抢走。"""
    mine = _filled(client, "keep")
    other = RedisTaskQueue("keep", client)
    held = mine.get_batch(10)

    stolen = 0
    for _ in range(4):
        time.sleep(LEASE * 0.6)
        mine.renew(held)  # 心跳线程在真实运行里做的事
        stolen += other.reclaim_expired()

    assert stolen == 0, f"健康节点被抢走了 {stolen} 个任务 —— 会被重抓一遍"


def test_silent_node_still_loses_its_tasks(client: Any) -> None:
    """不续期的节点（被 SIGKILL）任务仍要能被回收 —— 否则 v4.12 白做了。

    这条和上一条必须**同时**成立。只做到「健康节点不被抢」很容易滑向
    「续期太激进，死节点的任务也回收不了」，那是把问题挪了个位置。
    """
    dead = _filled(client, "dead", 3)
    other = RedisTaskQueue("dead", client)
    dead.get_batch(5)  # 领走后就不管了

    time.sleep(LEASE * 1.5)

    assert other.reclaim_expired() == 3


# ---- 续期的边界 ------------------------------------------------------
def test_renew_does_not_resurrect_reclaimed_tasks(client: Any) -> None:
    """已被别人回收走的任务，续期不能把它塞回在途表。

    用 `ZADD XX`（只更新已存在的成员）。不加 XX 的话，原节点一续期就把任务
    重新变成「自己持有」—— 而它同时又在别的节点手里，**两个节点都成了持有者**，
    比不续期更糟。
    """
    mine = _filled(client, "race", 2)
    other = RedisTaskQueue("race", client)
    held = mine.get_batch(5)

    time.sleep(LEASE * 1.5)
    assert other.reclaim_expired() == 2  # 任务已回到队列
    assert client.zcard("race:z_inflight") == 0

    renewed = mine.renew(held)

    assert renewed == 0, "把已经被回收的任务又塞回在途表了"
    assert client.zcard("race:z_inflight") == 0


def test_renew_skips_completed_tasks(client: Any) -> None:
    """已经销账的任务不该被续期复活。"""
    queue = _filled(client, "donecase", 3)
    held = queue.get_batch(5)
    queue.done(held[0])

    assert queue.renew(held) == 2
    assert client.zcard("donecase:z_inflight") == 2


def test_renew_is_a_noop_without_lease(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """关掉租约时不该有任何 Redis 往返 —— 那是给禁用脚本的 Redis 留的退路。"""
    queue = _filled(client, "nolease", 2)
    held = queue.get_batch(5)
    monkeypatch.setattr(setting, "SPIDER_TASK_LEASE", 0.0)
    monkeypatch.setattr(client, "register_script", _boom)

    assert queue.renew(held) == 0


def _boom(*_a: object, **_k: object) -> None:
    raise AssertionError("关掉租约后不该调用 Lua")


# ---- 续期要覆盖「正在处理的」，不只是缓冲区里的 ----------------------
def test_collector_reports_both_buffered_and_in_progress(client: Any) -> None:
    """正在被处理的任务恰恰是耗时最长、最容易超时的那批，必须一起续。"""
    from mineworker.core.collector import Collector

    queue = _filled(client, "held", 4)
    collector = Collector(queue)
    taken = collector.get_request(timeout=0)
    assert taken is not None

    held = collector.held_requests()

    assert taken in held, "正在处理的任务没被算进「本节点持有」"
    assert len(held) == 4, f"缓冲区里排队的没被算上（只报了 {len(held)} 个）"

    collector.done(taken)
    assert taken not in collector.held_requests(), "处理完了还报成持有"
