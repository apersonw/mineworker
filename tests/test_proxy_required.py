"""开着代理池却拿不到代理时，绝不能悄悄直连。

实测（本地靶子 + 会计数的真代理 + 会「断供」的取号 API，
`PROXY_ENABLE=True`、`PROXY_MAX_USE_TIMES=5`）：代理一用满就被丢弃、
取号接口没货了，**21 个请求里有 16 个从本机 IP 直接打到了靶子**，
没有日志、没有计数、没有报错。而开代理池的全部意义就是别这么干 ——
目标站看到的是真实 IP。
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from mineworker import setting
from mineworker.exceptions import ProxyUnavailableError
from mineworker.network import proxy_pool
from mineworker.network.circuit import counts_as_unhealthy
from mineworker.network.downloader._common import apick_proxy, pick_proxy
from mineworker.network.proxy_pool.base import ProxyPool
from mineworker.network.request import Request


class _Pool(ProxyPool):
    """按脚本给代理：给完就空。"""

    script: list[str | None] = []

    def get_proxy(self) -> str | None:
        return _Pool.script.pop(0) if _Pool.script else None

    def report_bad(self, proxy: str) -> None:
        return None


@pytest.fixture(autouse=True)
def _pool(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(setting, "PROXY_ENABLE", True)
    monkeypatch.setattr(setting, "PROXY_ALLOW_DIRECT", False)
    monkeypatch.setattr(setting, "PROXY_WAIT_TIMEOUT", 0.3)
    monkeypatch.setattr(setting, "PROXY_MIN_INTERVAL", 0.05)
    _Pool.script = []
    monkeypatch.setitem(proxy_pool._state, "pool", _Pool())
    yield
    monkeypatch.setitem(proxy_pool._state, "pool", None)


def _req() -> Request:
    return Request("http://example.com/p/1")


def test_no_proxy_means_no_request() -> None:
    """池空时抛错，而不是返回 None 让下载器直连。"""
    with pytest.raises(ProxyUnavailableError):
        pick_proxy(_req())


def test_allow_direct_is_an_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """确实想要「有代理就用、没有就直连」的，得自己把开关打开。"""
    monkeypatch.setattr(setting, "PROXY_ALLOW_DIRECT", True)
    assert pick_proxy(_req()) is None


def test_no_pool_configured_still_goes_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """压根没开代理池时，直连本来就是行为，不该报错。"""
    monkeypatch.setattr(setting, "PROXY_ENABLE", False)
    monkeypatch.setitem(proxy_pool._state, "pool", None)
    assert pick_proxy(_req()) is None


def test_explicit_proxy_wins_without_waiting() -> None:
    """请求自带代理时不该去碰代理池，更不该等。"""
    request = _req()
    request.requests_kwargs["proxy"] = "http://explicit:8080"
    started = time.monotonic()
    assert pick_proxy(request) == "http://explicit:8080"
    assert time.monotonic() - started < 0.2


def test_waits_for_stock_instead_of_failing_immediately() -> None:
    """供应商短暂断供不该把任务的重试次数耗光 —— 先等一会儿再重取。"""

    def restock() -> None:
        time.sleep(0.1)
        _Pool.script.append("http://restocked:8080")

    threading.Thread(target=restock, daemon=True).start()
    assert pick_proxy(_req()) == "http://restocked:8080"


def test_proxy_shortage_does_not_trip_the_circuit_breaker() -> None:
    """拿不到代理是我们自己的供应问题，不是目标站不健康。

    算进去的话，代理商断供五分钟就能把所有域全熔断一遍。
    """
    assert counts_as_unhealthy(ProxyUnavailableError("没货"), None) is False


def test_async_path_raises_too() -> None:
    with pytest.raises(ProxyUnavailableError):
        asyncio.run(apick_proxy(_req()))


def test_async_wait_does_not_block_the_event_loop() -> None:
    """异步路径必须 await：阻塞式 sleep 会把整个事件循环卡住，
    其它并发请求跟着一起停。"""

    async def scenario() -> int:
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        with pytest.raises(ProxyUnavailableError):
            await apick_proxy(_req())
        beat.cancel()
        return ticks

    assert asyncio.run(scenario()) > 3, "等代理期间事件循环停摆了"
