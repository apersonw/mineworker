"""分层布隆在**真 Redis + 真多进程**下的一致性（v4.5 阶段 C）。

fakeredis + 单进程测不出这里的东西：`add()` 的原子性只有在真正并发时才有意义，
而层数是由共享计数器推导的 —— 多个节点同时越过层边界才是真正的考验。

判据都是可数的事实（`add()` 返回 True 的总次数、能不能查得到），不问过滤器自己。
"""

from __future__ import annotations

import multiprocessing as mp
import os
from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.integration

_PROC_TIMEOUT = 60

#: 误判率取到极低，好让下面的**严格相等**断言有意义：`1e-4` 时 900 次插入平均就会
#: 有约 0.1 次固有误判（实测单进程也会丢 1 个），断言必然偶发失败 ——
#: 而那种失败最容易被错怪到并发头上，白白查半天。
_ERR = 1e-9


@pytest.fixture
def clean_redis(redis_url: str) -> Iterator[str]:
    import redis as redis_lib

    client = redis_lib.from_url(redis_url, decode_responses=True)
    client.flushdb()
    yield redis_url
    client.flushdb()
    client.close()


# ---- 顶层定义，供子进程 spawn 后重新 import ---------------------------
def _insert_all(redis_url: str, name: str, keys: list[str], out: Any) -> None:
    """一个「节点」：把同一批 key 全部尝试插入，回报有多少次被判定为「新」。"""
    from mineworker import setting

    setting.REDIS_URL = redis_url
    from mineworker.dedup.redis_filter import RedisBloomFilter

    f = RedisBloomFilter(name, capacity=200, error_rate=_ERR, max_layers=4)
    out.put(sum(1 for k in keys if f.add(k)))


def _run(procs: list[mp.Process]) -> None:
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=_PROC_TIMEOUT)
    alive = [p for p in procs if p.is_alive()]
    for p in alive:
        p.terminate()
        p.join(timeout=5)
    assert not alive, "有子进程没有退出"


def test_concurrent_add_counts_each_key_exactly_once(clean_redis: str) -> None:
    """三个进程同时插入**同一批** key：「新」的判定总数必须恰好等于 key 数。

    这是原子性的直接检验。改成 Lua 之前是 pipeline 的 SETBIT —— pipeline 只是
    打包发送、并不保证原子，两个节点会同时把一个 key 判定为「新」，于是同一个
    URL 被抓两遍、计数也偏高。fakeredis 单进程永远测不出这个。
    """
    keys = [f"k{i}" for i in range(600)]  # 超过基础容量 200，会跨层
    name = f"dedup{os.getpid()}"
    out: Any = mp.Queue()
    _run([mp.Process(target=_insert_all, args=(clean_redis, name, keys, out)) for _ in range(3)])

    fresh_total = sum(out.get() for _ in range(3))
    assert fresh_total == len(keys), (
        f"「新」判定了 {fresh_total} 次，应为 {len(keys)} 次 —— 同一个 URL 会被抓多遍"
    )


def test_layers_stay_consistent_across_processes(clean_redis: str) -> None:
    """各进程插各自的 key，之后任一节点都要认得**全部** key。

    层数由共享计数器推导，多个进程会在不同时刻越过层边界；
    只要读的时候把 0..top 全查，落在哪一层都不影响结果。
    """
    name = f"dedup{os.getpid()}x"
    groups = [[f"g{g}-{i}" for i in range(300)] for g in range(3)]
    out: Any = mp.Queue()
    _run([mp.Process(target=_insert_all, args=(clean_redis, name, g, out)) for g in groups])

    fresh_total = sum(out.get() for _ in range(3))
    assert fresh_total == sum(len(g) for g in groups), "有 key 被误判成已存在（漏抓）"

    from mineworker import setting
    from mineworker.dedup.redis_filter import RedisBloomFilter

    setting.REDIS_URL = clean_redis
    late = RedisBloomFilter(name, capacity=200, error_rate=_ERR, max_layers=4)
    assert late.count == 900, f"共享计数 {late.count}，应为 900"
    missing = [k for g in groups for k in g if k not in late]
    assert not missing, f"后加入的节点认不出 {len(missing)} 个已抓过的 key —— 会重复抓"
