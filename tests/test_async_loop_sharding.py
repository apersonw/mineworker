"""异步下载器的事件循环也要分片。

「一个事件循环线程 + N 个线程阻塞提交」这个模式在 N 超过 ~24 时**坍塌**
（50ms 靶子、实测）：

    线程        8    16    20    24    32    48
    QPS       143   281   342   328    92    60
    均在途    7.4  14.4  17.5  16.8   4.7   3.1

**坍塌与本框架的逻辑无关。** 把框架整个拿掉、只留一个 loop + N 个线程反复
`run_coroutine_threadsafe(client.get(url), loop).result()`，数字一模一样
（32 线程 89 QPS / 在途 4.6）。所以在框架里改逻辑救不了 —— 只能多开几个循环。

按 16 线程一个循环分片后（裸模式）：32 线程 506 QPS（5.7×）、48 线程 349（5.5×）、
64 线程 259（4.5×）。

排除过的解释（都是实测排除，不是想当然）：
- 「没跑完 / 失败重试」→ 800/800 全成功、零重试，墙钟真的从 3.7s 涨到 11.5s
- 「GIL convoy」→ `sys.setswitchinterval` 改 100 倍，三档数字几乎完全一致
- 「worker 线程解析抢走 GIL」→ 预言是加重解析会更差，实测**反而更好**，猜想作废
"""

from __future__ import annotations

import threading

import pytest

from mineworker import setting
from mineworker.network.downloader._async_httpx import AsyncHttpxDownloader, loop_count


def test_loop_count_follows_thread_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "ASYNC_THREADS_PER_LOOP", 16)
    for threads, expected in ((1, 1), (4, 1), (16, 1), (17, 2), (32, 2), (48, 3), (64, 4)):
        monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", threads)
        assert loop_count() == expected, f"{threads} 线程该有 {expected} 个循环"


def test_sharding_off_by_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ASYNC_THREADS_PER_LOOP=0` 回到单个事件循环。"""
    monkeypatch.setattr(setting, "ASYNC_THREADS_PER_LOOP", 0)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 256)
    assert loop_count() == 1


def test_at_or_below_the_knee_stays_single_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """线程数不超过一片的容量时只开一个循环 —— 对现有部署零影响。

    框架默认 `SPIDER_THREAD_COUNT=4`，落在这一档。
    """
    monkeypatch.setattr(setting, "ASYNC_THREADS_PER_LOOP", 16)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 4)
    dl = AsyncHttpxDownloader(concurrency=8)
    try:
        assert len(dl._shards) == 1
    finally:
        dl.close()


def test_threads_land_on_different_loops() -> None:
    """多个线程要真的落到不同的循环上，否则分了等于没分。"""
    dl = AsyncHttpxDownloader(concurrency=8, loops=4)
    seen: set[int] = set()
    lock = threading.Lock()

    def worker() -> None:
        shard = dl._shard()
        with lock:
            seen.add(id(shard))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(seen) > 1, "16 个线程全挤在一个事件循环上，分片没生效"
        assert len(seen) <= 4
    finally:
        dl.close()


def test_each_shard_has_its_own_loop_and_client() -> None:
    """每片必须是**独立**的循环和 client。

    `httpx.AsyncClient` 绑定在创建它的事件循环上，两片共用一个 client
    就会把它挂到别的 loop 上去。
    """
    dl = AsyncHttpxDownloader(concurrency=8, loops=3)
    try:
        assert len({id(s.loop) for s in dl._shards}) == 3
        assert len({id(s.client) for s in dl._shards}) == 3
        assert len({id(s.proxied) for s in dl._shards}) == 3
    finally:
        dl.close()


def test_close_collects_every_loop_thread() -> None:
    """**每一片都要收** —— 漏掉一片就是漏掉一个事件循环线程和它全部的连接。"""
    dl = AsyncHttpxDownloader(concurrency=8, loops=3)
    alive = [t for t in threading.enumerate() if t.name.startswith("async-downloader")]
    assert len(alive) == 3

    dl.close()
    left = [
        t for t in threading.enumerate() if t.name.startswith("async-downloader") and t.is_alive()
    ]
    assert not left, f"关完还剩 {len(left)} 个循环线程没收"


def test_close_is_idempotent_across_shards() -> None:
    dl = AsyncHttpxDownloader(concurrency=8, loops=2)
    dl.close()
    dl.close()  # 不抛
