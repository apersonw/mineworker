"""httpx 连接池上限必须跟着并发走。

httpx 的默认值是 ``max_connections=100`` 配 ``max_keepalive_connections=20``
—— 两者不等。并发一超过 20，多出来的连接每轮用完就被关掉、下轮再重建；
走代理 + https 时每次重建 = 一次 CONNECT 隧道 + 一次完整 TLS 握手，
上一程加的「按代理复用连接」在这里等于白做。

实测（32 线程、https 靶子、本机 tinyproxy、7 轮、只改 keepalive 这一个值）：
    keepalive = 20（httpx 默认）   281 QPS
    keepalive = 100               499 QPS
默认配置下还观察到一个近乎串行的坏模式（~17 QPS），配了上限后 14 轮没再出现。

这里断言的是**构造出来的 client 池子多大**，不是源码里有没有那个词 ——
之前用字符串匹配验过一个容器，匹到的是注释，假阴性。
"""

from __future__ import annotations

import httpx
import pytest

from mineworker import setting
from mineworker.network.downloader._async_httpx import AsyncHttpxDownloader
from mineworker.network.downloader._common import pool_limits
from mineworker.network.downloader._httpx import HttpxDownloader


def _pool_of(client: httpx.Client | httpx.AsyncClient) -> tuple[int, int]:
    """从 client 里掏出实际生效的 (max_connections, max_keepalive)。"""
    pool = client._transport._pool  # type: ignore[attr-defined]
    return pool._max_connections, pool._max_keepalive_connections


def test_sync_client_keepalive_follows_thread_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 64)
    downloader = HttpxDownloader(use_session=True)
    client = downloader._make_client(None, True)
    try:
        max_conn, keepalive = _pool_of(client)
        # 关键断言：能同时保活的连接数不少于线程数。少了就意味着
        # 一部分线程的连接每轮都要重建，「复用」名存实亡
        assert keepalive >= 64, f"keepalive={keepalive} 小于线程数 64"
        assert max_conn >= keepalive
    finally:
        client.close()
        downloader.close()


def test_proxied_client_gets_limits_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """带代理的那条路径也要配 —— 它才是真正吃亏的那条。"""
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 48)
    downloader = HttpxDownloader(use_session=True)
    client = downloader._make_client("http://127.0.0.1:9", True)
    try:
        _, keepalive = _pool_of(client)
        assert keepalive >= 48
    finally:
        client.close()
        downloader.close()


def test_async_per_proxy_client_gets_limits() -> None:
    """异步下载器的主 client 一直配着 limits，「每代理」那条却没有。

    同一个文件里两条构造路径不一致，而开代理池时走的恰恰是没配的那条。
    """
    downloader = AsyncHttpxDownloader(concurrency=40)
    try:
        client = downloader._submit(downloader._client_for_proxy("http://127.0.0.1:9", False))
        _, keepalive = _pool_of(client)
        assert keepalive >= 40, f"每代理 client 的 keepalive={keepalive}，没跟上并发 40"
    finally:
        downloader.close()


def test_never_tightens_below_httpx_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """并发比 httpx 默认值还小时，不能反过来把池子收紧。

    没有这一条的话，`SPIDER_THREAD_COUNT=4`（框架默认）会把 max_connections
    从 100 压到 4 —— 修一个性能问题的同时给小并发部署造一个新的。
    """
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 4)
    limits = pool_limits()
    assert limits.max_connections is not None and limits.max_connections >= 100
    assert limits.max_keepalive_connections is not None and limits.max_keepalive_connections >= 20
