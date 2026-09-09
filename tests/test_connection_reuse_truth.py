"""连接复用由**服务端计数**判定，不数对象。

起因：`test_session_reuse.py` 的每一条断言数的都是对象（建了几个 client、
有没有 close），而 `test_end_to_end_session_reuse` 更是只断言 body 是 "pong" ——
把连接复用整个关掉，两者照样全绿。

代价是真实的：**curl 下载器从来没有复用过一条连接**，这个事实瞒过了两个发布版本，
因为覆盖它的用例数的是「建了几个 Session」。这里换成「服务端接受了几条 TCP 连接」，
框架怎么想都不影响这个判据。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from conn_counting_server import ConnCountingServer
from mineworker import setting
from mineworker.network.downloader import close_default_downloaders
from mineworker.network.downloader._async_httpx import AsyncHttpxDownloader
from mineworker.network.downloader._httpx import HttpxDownloader
from mineworker.network.request import Request

N = 6


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    yield
    close_default_downloaders()


@pytest.fixture
def server() -> Iterator[ConnCountingServer]:
    with ConnCountingServer() as srv:
        yield srv


def _hit(downloader: object, url: str, n: int = N, **kw: object) -> None:
    for _ in range(n):
        resp = downloader.download(Request(url, **kw))  # type: ignore[attr-defined]
        assert resp.status_code == 200


def test_the_counting_server_can_actually_see_reuse(server: ConnCountingServer) -> None:
    """阳性 + 阴性对照：先证明这个靶子分得出复用与不复用。

    没有这一条，下面所有断言都可能是「测量看不见」而不是「行为正确」。
    """
    import httpx

    with httpx.Client() as c:  # 一个 client 连发 N 次 → 应该只开 1 条
        for _ in range(N):
            c.get(server.url)
    assert server.connections == 1, f"复用时开了 {server.connections} 条，靶子计数不可信"

    for _ in range(N):  # 每次新 client → 应该开 N 条
        with httpx.Client() as c:
            c.get(server.url)
    assert server.connections == 1 + N, "不复用时的连接数对不上，靶子计数不可信"


def test_httpx_session_reuses_one_connection(server: ConnCountingServer) -> None:
    """开 session：N 个请求只该开 1 条连接。

    这是 v4.26/v4.27 那套「按代理复用连接」真正声称的东西。
    """
    dl = HttpxDownloader(use_session=True)
    try:
        _hit(dl, server.url, use_session=True)
    finally:
        dl.close()
    assert server.requests == N
    assert server.connections == 1, f"{N} 个请求开了 {server.connections} 条连接，没有复用"


def test_httpx_without_session_opens_one_per_request(server: ConnCountingServer) -> None:
    """不开 session：每个请求一条连接。

    这条是上一条的**阴性对照** —— 少了它，上一条即使在「靶子总是只报 1」的
    坏情况下也会绿。
    """
    dl = HttpxDownloader(use_session=False)
    try:
        _hit(dl, server.url, use_session=False)
    finally:
        dl.close()
    assert server.connections == N, f"不开 session 却只开了 {server.connections} 条"


def test_async_downloader_reuses_one_connection(server: ConnCountingServer) -> None:
    """异步下载器同样要复用。"""
    dl = AsyncHttpxDownloader(concurrency=4, loops=1)
    try:
        _hit(dl, server.url)
    finally:
        dl.close()
    assert server.connections == 1, f"async 下载器 {N} 个请求开了 {server.connections} 条"


def test_curl_session_does_not_reuse_and_that_is_documented(
    server: ConnCountingServer,
) -> None:
    """**钉住 curl 的已知限制**：开了 session 也没有连接复用。

    框架的 curl 路径永远用 `stream=True`（`MAX_RESPONSE_SIZE` 要边读边判），
    而 curl_cffi 的 `Response._finalize_stream()` 收尾时 `self.curl.close()` ——
    关掉的是整个 Curl 句柄，连接缓存随之消失。

    这条断言的是**现状**而不是期望。哪天 curl_cffi 改了行为、或者框架不再
    无条件 stream，它会红 —— 那时该做的是更新文档里「curl 不复用连接」那句话，
    而不是把这条用例删掉。
    """
    pytest.importorskip("curl_cffi")
    from mineworker.network.downloader._curl import CurlDownloader

    dl = CurlDownloader(use_session=True, impersonate="chrome")
    try:
        _hit(dl, server.url, use_session=True)
    finally:
        dl.close()
    assert server.requests == N
    assert server.connections == N, (
        f"curl 竟然复用了连接（{server.connections} 条 / {N} 个请求）—— "
        "如果 curl_cffi 或框架改了行为，请同步更新 docs/settings.md 里"
        "「use_session 在 curl 这边只带来 cookie 持久化」那一段"
    )


def test_sharded_sessions_still_reuse_within_a_shard(
    server: ConnCountingServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分片不能把复用也切没了：单线程连发 N 次仍然只落在一片上，只开 1 条连接。

    v4.33 给连接池分片时，如果分片键算错（比如每次请求都换一片），
    对象计数完全看不出来，但连接数会立刻暴露。
    """
    monkeypatch.setattr(setting, "SESSION_SHARD_THREADS", 1)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 8)  # → 8 片
    dl = HttpxDownloader(use_session=True)
    try:
        _hit(dl, server.url, use_session=True)
    finally:
        dl.close()
    assert server.connections == 1, (
        f"同一个线程连发 {N} 次却开了 {server.connections} 条 —— 分片键跟着请求变了"
    )
