"""`RequestBuffer.flush()` 里挨着的两次 Redis 调用，失败时都不能丢请求。

那个循环里紧挨着两句都会走网络：`dedup.add()`（写指纹）和 `queue.put()`（入队）。
v4.7 把 `put` 包进了 try，`add` 留在了外面 —— 同一种抖动、同一种后果。

实测（10 个请求、第 3 个上抖一次）：
    抖在 put        已入队 2、放回 8、丢失 0
    抖在 dedup.add  已入队 2、放回 0、**丢失 8**

顺带一处相反方向的错：`_requeue` 把 `batch[i:]` 全部清了 `filter_repeat`，
但只有第一条真的过了去重。实测批次里两条相同 URL、`put` 抖一次，
**同一个 URL 进队列 2 次**。
"""

from __future__ import annotations

from typing import Any

import pytest

from mineworker.buffer.request_buffer import RequestBuffer
from mineworker.network.request import Request
from mineworker.utils.stats import Stats


class _Dedup:
    def __init__(self, boom_at: int | None = None) -> None:
        self.seen: set[str] = set()
        self.calls = 0
        self.boom_at = boom_at

    def add(self, fingerprint: str) -> bool:
        self.calls += 1
        if self.calls == self.boom_at:
            raise ConnectionError("Redis 断了")
        if fingerprint in self.seen:
            return False
        self.seen.add(fingerprint)
        return True

    def get(self, fingerprint: str) -> bool:
        return fingerprint in self.seen


class _Queue:
    def __init__(self, boom_at: int | None = None) -> None:
        self.got: list[Request] = []
        self.calls = 0
        self.boom_at = boom_at

    def put(self, request: Request) -> None:
        self.calls += 1
        if self.calls == self.boom_at:
            self.boom_at = None  # 只抖一次
            raise ConnectionError("Redis 断了")
        self.got.append(request)


def _buffer(queue: Any, dedup: Any) -> RequestBuffer:
    return RequestBuffer(queue, Stats(), dedup=dedup)


def test_dedup_failure_does_not_lose_the_rest() -> None:
    """写指纹抖一下，剩下的请求必须回到缓冲区 —— 否则它们既不在队列、
    也不在缓冲区、也没落盘。"""
    queue, dedup = _Queue(), _Dedup(boom_at=3)
    buffer = _buffer(queue, dedup)
    for i in range(10):
        buffer.put(Request(f"http://example.com/p/{i}"))

    with pytest.raises(ConnectionError):
        buffer.flush()

    assert len(queue.got) + buffer.pending_count() == 10, (
        f"入队 {len(queue.got)}、放回 {buffer.pending_count()}，"
        f"丢了 {10 - len(queue.got) - buffer.pending_count()} 个"
    )


def test_request_that_failed_dedup_keeps_its_flag() -> None:
    """写指纹没成功的那条，放回时必须保留 `filter_repeat` —— 它还没查过重。"""
    queue, dedup = _Queue(), _Dedup(boom_at=1)
    buffer = _buffer(queue, dedup)
    buffer.put(Request("http://example.com/p/1"))

    with pytest.raises(ConnectionError):
        buffer.flush()

    (back,) = buffer.drain_pending()
    assert back.filter_repeat is True, "指纹没写成却当成过了去重，重复 URL 会漏过去"


def test_put_failure_clears_the_flag_on_the_first_only() -> None:
    """入队失败的那条**写过**指纹，必须清掉标志（否则被自己刚写的指纹挡住）；
    但它后面那些根本没走到去重，必须保留。"""
    queue, dedup = _Queue(boom_at=2), _Dedup()
    buffer = _buffer(queue, dedup)
    for i in range(4):
        buffer.put(Request(f"http://example.com/p/{i}"))

    with pytest.raises(ConnectionError):
        buffer.flush()

    back = buffer.drain_pending()
    assert back[0].filter_repeat is False, "指纹已写却没清标志 —— 下一轮会被自己挡掉"
    assert all(r.filter_repeat for r in back[1:]), (
        "没查过重的请求被清了标志 —— 它们会绕过去重进队列"
    )


def test_duplicate_still_blocked_after_a_put_failure() -> None:
    """别用「全都清掉」换「不丢请求」—— 重复 URL 仍然要挡住。"""
    queue, dedup = _Queue(boom_at=2), _Dedup()
    buffer = _buffer(queue, dedup)
    for url in ["http://x/a", "http://x/dup", "http://x/dup", "http://x/b"]:
        buffer.put(Request(url))

    with pytest.raises(ConnectionError):
        buffer.flush()
    buffer.flush()  # 下一轮重试

    urls = [r.url for r in queue.got]
    assert urls.count("http://x/dup") == 1, f"同一个 URL 进队列多次：{urls}"
    assert len(queue.got) == 3


def test_put_failure_still_loses_nothing() -> None:
    """v4.7 修好的那条不能被这次改动弄坏。"""
    queue, dedup = _Queue(boom_at=3), _Dedup()
    buffer = _buffer(queue, dedup)
    for i in range(10):
        buffer.put(Request(f"http://example.com/p/{i}"))

    with pytest.raises(ConnectionError):
        buffer.flush()
    assert len(queue.got) + buffer.pending_count() == 10
