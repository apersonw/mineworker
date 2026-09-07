"""`RequestBuffer` 在队列出错时不能丢请求、也不能把自己线程搞死（v4.7）。

原来 `flush()` 先把整批从 `_pending` 摘走再逐个 put —— 队列一次抖动
（Redis 断连 / OOM / 网络闪断），剩下的请求既不在 `_pending`、也没进队列、
也没像 Item 那样 dump 到文件，**静默消失**。实测 10 个请求、第 3 个上抖一次：丢 8 个。
而且 `run()` 里没有 try/except，后台线程会直接死掉，此后**所有**请求都不再入队。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from mineworker.buffer.request_buffer import RequestBuffer
from mineworker.network.request import Request
from mineworker.utils.stats import Stats


class FlakyQueue:
    """第 ``fail_on`` 次 put 抛一次异常，之后恢复 —— 模拟队列一次抖动。"""

    def __init__(self, fail_on: int = 3) -> None:
        self.got: list[str] = []
        self.n = 0
        self.fail_on = fail_on

    def put(self, request: Request) -> None:
        self.n += 1
        if self.n == self.fail_on:
            raise ConnectionError("Redis 连接重置")
        self.got.append(request.url)


class RecordingDedup:
    """真去重的行为：同一个指纹只放行一次。"""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def add(self, key: str) -> bool:
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


def _buffer(queue: Any, dedup: Any = None) -> RequestBuffer:
    return RequestBuffer(queue, Stats(), dedup=dedup or RecordingDedup())


# ---- A：不丢 ---------------------------------------------------------
def test_queue_error_keeps_requests_in_the_buffer() -> None:
    """抖动时没入队的请求要留在缓冲区，一个都不能少。"""
    q = FlakyQueue(fail_on=3)
    buf = _buffer(q)
    urls = [f"http://site/{i}" for i in range(10)]
    for u in urls:
        buf.put(Request(u))

    with pytest.raises(ConnectionError):
        buf.flush()

    still_here = [r.url for r in buf.drain_pending()]
    assert set(q.got) | set(still_here) == set(urls), "有请求既没入队也不在缓冲区 —— 丢了"
    assert len(q.got) + len(still_here) == len(urls), "总数对不上"


def test_retry_after_a_blip_enqueues_everything() -> None:
    """抖动恢复后，下一轮 flush 必须把剩下的全部入队。

    这里最容易栽的地方：`dedup.add()` 是在入队**之前**写指纹的，所以放回缓冲区
    的请求在重试时会撞上自己刚写下的那条指纹，被当成重复丢弃 —— 等于没救回来。
    用会真实记忆的 `RecordingDedup` 才测得出这一点。
    """
    q = FlakyQueue(fail_on=3)
    buf = _buffer(q)
    urls = [f"http://site/{i}" for i in range(10)]
    for u in urls:
        buf.put(Request(u))

    with pytest.raises(ConnectionError):
        buf.flush()
    buf.flush()  # 队列已恢复

    assert sorted(q.got) == sorted(urls), (
        f"恢复后只入队了 {len(q.got)}/{len(urls)} 个 —— 放回去的请求被自己写下的去重指纹挡掉了"
    )
    assert buf.drain_pending() == []


def test_genuine_duplicates_are_still_dropped() -> None:
    """救回请求不能把去重一起废掉：真正重复的还是要丢。"""

    class Healthy:
        def __init__(self) -> None:
            self.got: list[str] = []

        def put(self, request: Request) -> None:
            self.got.append(request.url)

    q = Healthy()
    buf = _buffer(q)
    for _ in range(3):
        buf.put(Request("http://site/same"))
    buf.flush()

    assert q.got == ["http://site/same"], "去重失效了"


# ---- B：不死 ---------------------------------------------------------
def test_background_thread_survives_a_queue_error() -> None:
    """后台 flush 线程不能被一次抖动打死。

    原来 `run()` 是裸调 `flush()` —— 线程一死，此后**所有**请求都不再进队列，
    爬虫静默停止发现新页面，而且没有任何报错。
    """
    q = FlakyQueue(fail_on=1)
    buf = _buffer(q)
    buf.start()
    try:
        buf.put(Request("http://site/x"))
        time.sleep(0.5)
        assert buf.is_alive(), "线程被一次队列抖动打死了"
        # 抖动过去之后要能自愈
        deadline = time.monotonic() + 3
        while not q.got and time.monotonic() < deadline:
            time.sleep(0.05)
        assert q.got == ["http://site/x"], "抖动之后没有自愈重试"
    finally:
        buf.stop()
        buf.join(timeout=5)
