"""连接池缓存的「取或建」必须是原子的。

原来是无锁的「查 → 建 → 存」：N 个线程首次碰到同一个代理，全都查不到、
全都建一个。**实测 1 个代理 8 线程建了 8 个 client、32 线程建了 32 个** ——
「按代理复用连接」在每个代理的第一轮就先浪费 N-1 个连接池。

更糟的是那 N-1 个的下场：`put()` 同 key 覆盖时把被顶掉的那个**既不返回也不关闭**，
而它的文档恰恰承诺「返回被换出、需要关闭的对象」。

⚠️ 修法不能是「让 put 返回被顶掉的那个」：抢输的线程正拿着它发请求，
关掉就是 use-after-close。查和建必须在同一把锁里，抢输者关掉的是自己建的那个。

这里用真线程 + Barrier 制造竞争，不是模拟 —— 竞态用例不真并发就等于没写。
"""

from __future__ import annotations

import threading
import time

import pytest

from mineworker import setting
from mineworker.network.downloader._common import ProxyClientCache


class _FakeClient:
    def __init__(self, serial: int) -> None:
        self.serial = serial
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _race(cache: ProxyClientCache, key: str | None, n: int) -> tuple[list, list]:
    """n 个线程同时对同一个 key 调 get_or_create。返回 (建出来的全部, 每个线程拿到的)。"""
    built: list[_FakeClient] = []
    got: list[_FakeClient] = []
    build_lock = threading.Lock()
    barrier = threading.Barrier(n)

    def factory() -> _FakeClient:
        # **这个 sleep 是必需的**：真实的 httpx.Client(...) 构造约 0.1ms 且会放开
        # GIL，那才是竞争窗口。第一版 factory 只有几条字节码，窗口小到 GIL 压根
        # 不切换 —— 于是把锁整个去掉，用例照样连跑三次全绿，测了个寂寞。
        time.sleep(0.005)
        with build_lock:
            client = _FakeClient(len(built))
            built.append(client)
            return client

    def worker() -> None:
        barrier.wait()  # 尽量让所有线程同时抵达
        client, evicted = cache.get_or_create(key, factory)
        for old in evicted:
            old.close()
        with build_lock:
            got.append(client)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert all(not t.is_alive() for t in threads), "有线程没退出 —— 大概率死锁了"
    return built, got


def test_concurrent_first_touch_builds_exactly_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """32 个线程同时首次碰到同一个代理，只该建 1 个。"""
    monkeypatch.setattr(setting, "SESSION_CACHE_SIZE", 16)
    cache = ProxyClientCache()
    built, got = _race(cache, "http://p:1", 32)

    assert len(built) == 1, f"建了 {len(built)} 个 client，取或建不是原子的"
    assert len({id(c) for c in got}) == 1, "线程之间拿到了不同的 client"
    assert len(cache) == 1


def test_no_client_is_silently_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个建出来的对象，要么还在缓存里，要么已经被关掉 —— 不许悄悄漏掉。

    这一条即使 get_or_create 退化成「每次都建」也该成立（那时全部会被换出并关闭），
    所以它抓的是**资源有没有被交代**，与上一条抓的原子性互补。
    """
    monkeypatch.setattr(setting, "SESSION_CACHE_SIZE", 1)
    cache = ProxyClientCache()
    built: list[_FakeClient] = []

    for i in range(5):
        client, evicted = cache.get_or_create(f"http://p:{i}", lambda i=i: _FakeClient(i))  # type: ignore[misc]
        built.append(client)
        for old in evicted:
            old.close()

    alive = set(map(id, cache.drain()))
    for client in built:
        assert client.closed or id(client) in alive, f"client#{client.serial} 既没关也不在缓存里"


def test_eviction_still_hands_back_what_must_be_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超出 SESSION_CACHE_SIZE 时，被挤出去的要交还给调用方关闭。

    缓存自己不关：同步要 close()、异步要 await aclose()，
    它没法同时服务两种。
    """
    monkeypatch.setattr(setting, "SESSION_CACHE_SIZE", 2)
    cache = ProxyClientCache()
    first, _ = cache.get_or_create("http://p:1", lambda: _FakeClient(1))
    cache.get_or_create("http://p:2", lambda: _FakeClient(2))
    _, evicted = cache.get_or_create("http://p:3", lambda: _FakeClient(3))

    assert [c.serial for c in evicted] == [first.serial], "最久没用的那个没被交出来"
    assert len(cache) == 2


def test_cache_hit_does_not_call_the_factory() -> None:
    """命中时不许再造一个 —— 否则「复用」只是名字好听。"""
    cache = ProxyClientCache()
    calls = []

    def factory() -> _FakeClient:
        calls.append(1)
        return _FakeClient(len(calls))

    cache.get_or_create("http://p:1", factory)
    cache.get_or_create("http://p:1", factory)
    assert len(calls) == 1
