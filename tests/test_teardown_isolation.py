"""关闭序列里一步失败，不能带走后面的步骤。

`_teardown()` 原来是一串裸调用，而最关键的两步排在后面：
把内存里的数据落库、把持有的任务推回队列（分布式下还要释放租约）。

关机时 Redis 抖一下，第一步 `request_buffer.flush()` 就会抛 ——
实测数据没落库、任务也没推回。**最可能抛的那一步，正好排在它们前面。**
"""

from __future__ import annotations

from typing import Any

import pytest

from mineworker.core import base_scheduler as bs
from mineworker.core.base_scheduler import BaseScheduler


class _Buffer:
    def __init__(self, boom: str | None = None) -> None:
        self.boom = boom
        self.done: list[str] = []

    def _step(self, name: str) -> None:
        if self.boom == name:
            raise ConnectionError("Redis 断了")
        self.done.append(name)

    def stop(self) -> None:
        self._step("stop")

    def join(self, timeout: float | None = None) -> None:
        self._step("join")

    def flush(self) -> None:
        self._step("flush")

    def close(self) -> None:
        self._step("close")


def _scheduler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    req_boom: str | None = None,
    item_boom: str | None = None,
    downloader_boom: bool = False,
) -> tuple[Any, list[str], _Buffer]:
    calls: list[str] = []
    scheduler = BaseScheduler.__new__(BaseScheduler)
    scheduler._workers = []
    scheduler._metrics = None
    scheduler._request_buffer = _Buffer(boom=req_boom)
    item_buffer = _Buffer(boom=item_boom)
    scheduler._item_buffer = item_buffer
    scheduler._user_pool = None
    scheduler._restore_signal = lambda: calls.append("restore_signal")
    scheduler._on_shutdown = lambda: calls.append("on_shutdown")

    def _downloaders() -> None:
        if downloader_boom:
            raise RuntimeError("浏览器关不掉")
        calls.append("close_downloaders")

    monkeypatch.setattr(bs, "close_default_downloaders", _downloaders)
    monkeypatch.setattr(bs, "close_proxy_pool", lambda: calls.append("close_proxy"))
    return scheduler, calls, item_buffer


def test_baseline_runs_every_step(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler, calls, item_buffer = _scheduler(monkeypatch)
    scheduler._teardown()
    assert "flush" in item_buffer.done
    assert calls == ["restore_signal", "close_downloaders", "close_proxy", "on_shutdown"]


def test_request_flush_failure_does_not_skip_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    """关机时 Redis 抖动最可能命中这一步，而它排在落库和任务推回前面。"""
    scheduler, calls, item_buffer = _scheduler(monkeypatch, req_boom="flush")
    scheduler._teardown()
    assert "flush" in item_buffer.done, "请求推送失败，内存里的数据就不落库了"
    assert "on_shutdown" in calls, "请求推送失败，持有的任务就不推回队列了"


def test_item_flush_failure_does_not_skip_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler, calls, _ = _scheduler(monkeypatch, item_boom="flush")
    scheduler._teardown()
    assert "on_shutdown" in calls
    assert "close_downloaders" in calls


def test_downloader_close_failure_does_not_skip_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """浏览器关不掉不该连累任务推回。"""
    scheduler, calls, _ = _scheduler(monkeypatch, downloader_boom=True)
    scheduler._teardown()
    assert "close_proxy" in calls
    assert "on_shutdown" in calls


def test_buffer_stop_failure_does_not_skip_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler, calls, item_buffer = _scheduler(monkeypatch, req_boom="stop")
    scheduler._teardown()
    assert "flush" in item_buffer.done
    assert "on_shutdown" in calls


def test_order_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """落库必须排在任务推回之前 —— flush 会把已落库请求从「持有中」摘掉，
    否则推回时会把它们又推一遍。"""
    order: list[str] = []
    scheduler, _calls, item_buffer = _scheduler(monkeypatch)
    original = item_buffer.flush
    item_buffer.flush = lambda: (order.append("item_flush"), original())[1]  # type: ignore[method-assign]
    scheduler._on_shutdown = lambda: order.append("on_shutdown")
    scheduler._teardown()
    assert order == ["item_flush", "on_shutdown"]
