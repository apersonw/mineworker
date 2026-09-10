"""一个代理的连接池要分片，但 cookie 不能跟着被切开。

**为什么要分片**：一个 `httpx.Client` 被太多线程共用时，连接池自己成为争用点。
实测（https、单代理、靶子天花板已用裸 asyncio 确认远高于被测）：

    线程        16    32    48    64    96
    共用一个   269   444   248   172   105     ← ~32 见顶后掉头向下
    分片       269   451   563   492   434
    每请求新建 173   203   243   249   265

48 线程往上，共用一个 client 比「每请求新建」还慢 —— v4.26/v4.27 那套复用
在这一段是负收益。

**为什么 cookie 不能切开**：`docs/settings.md` 对 `USE_SESSION` 的承诺是
「复用 httpx 连接（连同 cookie jar）」。分片天然会把 jar 切开 ——
登录态落在 0 号片，下个请求走 1 号片就没了。
所以同一代理的所有分片共用一个 `CookieJar`（httpx 收到裸 CookieJar 时按引用使用）。
"""

from __future__ import annotations

import threading
from http.cookiejar import CookieJar

import pytest

from mineworker import setting
from mineworker.network.downloader._common import shard_count, shard_index
from mineworker.network.downloader._httpx import HttpxDownloader


def test_shard_count_follows_thread_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "SESSION_SHARD_THREADS", 32)
    for threads, expected in ((1, 1), (4, 1), (32, 1), (33, 2), (64, 2), (96, 3)):
        monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", threads)
        assert shard_count() == expected, f"{threads} 线程该有 {expected} 片"


def test_sharding_off_by_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SESSION_SHARD_THREADS=0` 关闭分片 —— 回到「每代理一个 client」。"""
    monkeypatch.setattr(setting, "SESSION_SHARD_THREADS", 0)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 256)
    assert shard_count() == 1


def test_at_or_below_the_knee_nothing_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """线程数不超过拐点时只有一片 —— 对现有部署零影响。

    框架默认 SPIDER_THREAD_COUNT=4，落在这一档。
    """
    monkeypatch.setattr(setting, "SESSION_SHARD_THREADS", 32)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 4)
    downloader = HttpxDownloader(use_session=True)
    try:
        a = downloader._session_client("http://p:1")
        b = downloader._session_client("http://p:1")
        assert a is b
        assert len(downloader._clients) == 1
    finally:
        downloader.close()


def test_threads_land_on_different_shards(monkeypatch: pytest.MonkeyPatch) -> None:
    """多个线程要真的落到不同分片上 —— 否则分了等于没分。"""
    monkeypatch.setattr(setting, "SESSION_SHARD_THREADS", 4)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 16)  # → 4 片
    downloader = HttpxDownloader(use_session=True)
    seen: set[int] = set()
    lock = threading.Lock()

    def worker() -> None:
        client = downloader._session_client("http://p:1")
        with lock:
            seen.add(id(client))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(seen) > 1, "16 个线程全挤在同一个 client 上，分片没生效"
        assert len(seen) <= 4, f"分出了 {len(seen)} 个，超过 4 片"
    finally:
        downloader.close()


def test_all_shards_share_one_cookie_jar(monkeypatch: pytest.MonkeyPatch) -> None:
    """**核心用例**：写进一片的 cookie，另一片必须看得见。

    这是 USE_SESSION 的文档承诺（「连同 cookie jar」）。没有这一条，
    分片就是拿正确性换吞吐。
    """
    monkeypatch.setattr(setting, "SESSION_SHARD_THREADS", 1)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 4)  # → 4 片
    downloader = HttpxDownloader(use_session=True)
    try:
        clients = []
        barrier = threading.Barrier(4)

        def worker() -> None:
            barrier.wait()
            clients.append(downloader._session_client("http://p:1"))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        distinct = {id(c) for c in clients}
        assert len(distinct) > 1, "没分出多片，这条用例就没验到东西"

        # 往其中一片写 cookie，其余各片都要看得见
        clients[0].cookies.set("sid", "abc", domain="example.com")
        for client in clients[1:]:
            assert client.cookies.get("sid", domain="example.com") == "abc", (
                "分片之间 cookie 不通 —— USE_SESSION 的「连同 cookie jar」承诺被打破了"
            )
    finally:
        downloader.close()


def test_jar_is_per_proxy_not_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """不同代理之间 cookie 仍然隔离 —— 那是原本就有的行为，别顺手改掉。"""
    monkeypatch.setattr(setting, "SESSION_SHARD_THREADS", 32)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 4)
    downloader = HttpxDownloader(use_session=True)
    try:
        a = downloader._session_client("http://p:1")
        b = downloader._session_client("http://p:2")
        a.cookies.set("sid", "abc", domain="example.com")
        assert b.cookies.get("sid", domain="example.com") is None
    finally:
        downloader.close()


def test_httpx_still_takes_a_raw_jar_by_reference() -> None:
    """钉住这次修复依赖的 httpx 行为。

    `httpx.Cookies.__init__` 对**裸 CookieJar** 走 `else: self.jar = cookies`，
    即按引用使用。哪天 httpx 改成拷贝，分片就会静默地把 cookie 切开 ——
    这条用例要在那时立刻红，而不是等线上登录态莫名丢失。
    """
    import httpx

    jar = CookieJar()
    c1 = httpx.Client(cookies=jar)
    c2 = httpx.Client(cookies=jar)
    try:
        assert c1.cookies.jar is jar
        c1.cookies.set("k", "v", domain="example.com")
        assert c2.cookies.get("k", domain="example.com") == "v"
    finally:
        c1.close()
        c2.close()


def test_shard_index_is_stable_within_a_thread() -> None:
    """同一个线程每次都该落在同一片上，否则连接复用无从谈起。"""
    got = {shard_index(4) for _ in range(10)}
    assert len(got) == 1


def test_jar_outlives_its_clients_when_many_proxies_rotate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**凡是还有活 client 的代理，它的 jar 必须还在缓存里。**

    补的是一个真 bug：第一版 jar 缓存用的是默认的 `SESSION_CACHE_SIZE`，
    而 client 缓存是它的**片数倍** —— 代理一多，jar 先被换出、client 还活着，
    之后为同一代理新建分片就会拿到一个全新的 jar，两个分片的 cookie 从此分家。
    上面那些用例没抓到它，是因为它们只用了一两个代理，jar 缓存从没换出过。

    ⚠️ 这条断言的是**不变式**，不是「两次调用落在不同分片上」。
    第一版就是那么写的，结果**在修好的代码上也是红的** ——
    分片序号是每线程自增再取模，5 个线程在 2 片上是 0,1,0,1,0，
    第一次和最后一次必然同片。判据一旦依赖线程落点，就是在赌调度。
    """
    monkeypatch.setattr(setting, "SESSION_SHARD_THREADS", 1)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 3)  # → 3 片
    monkeypatch.setattr(setting, "SESSION_CACHE_SIZE", 2)  # client 容量 6
    downloader = HttpxDownloader(use_session=True)

    def touch(proxy: str) -> None:
        downloader._session_client(proxy)

    try:
        # **每个代理只碰一次**：这样 client 缓存的每个槽位对应一个不同的代理，
        # 活代理数 = client 容量（6）> 被削小的 jar 容量。
        # 第一版让每个代理碰三次，结果 6 个槽位被最后两个代理的三个分片占满 ——
        # 活代理只有 2 个，恰好等于 jar 容量，变异照样不红。
        for i in range(8):
            t = threading.Thread(target=touch, args=(f"http://p{i}",))
            t.start()
            t.join(timeout=10)

        live_clients = downloader._clients._items
        live_jars = downloader._jars._items
        assert live_clients, "一个 client 都没缓存，这条用例没验到东西"

        for key in live_clients:
            proxy = str(key).split("#", 1)[0]
            assert proxy in live_jars, (
                f"{proxy} 还有活着的 client，它的 jar 却已被换出 —— "
                "下次为它新建分片会拿到一个全新的 jar，登录态在分片之间丢失"
            )
    finally:
        downloader.close()


def test_hot_client_keeps_its_jar_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """**一个很热但从不重建的 client，它的 jar 不能因为「冷」被换出。**

    两个缓存的 LRU 位置更新时机不同：client 每次命中都会被挪到队尾，
    而 jar 如果只在**新建 client 时**才摸一下，就会一直停在队首 ——
    别的代理不断进来，它先被挤掉，而它的 client 还热着。

    这条和上一条各钉一个条件：上一条钉「容量要一样大」，这条钉「每次都要摸」。
    合成一条的话，任一变异都可能被另一条件掩盖。
    """
    monkeypatch.setattr(setting, "SESSION_SHARD_THREADS", 1)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 3)  # → 3 片
    monkeypatch.setattr(setting, "SESSION_CACHE_SIZE", 2)  # 两个缓存都是 6
    downloader = HttpxDownloader(use_session=True)
    hot = "http://hot"

    def touch(proxy: str) -> None:
        downloader._session_client(proxy)

    try:
        touch(hot)
        # 交替：不断刷新 hot 的 client，同时让新代理挤进来
        for i in range(8):
            touch(hot)
            touch(f"http://cold{i}")

        assert any(str(k).startswith(hot) for k in downloader._clients._items), (
            "hot 的 client 自己就被换出了，这条用例没验到东西"
        )
        assert hot in downloader._jars._items, (
            "hot 的 client 还热着，它的 jar 却被换出了 —— "
            "jar 的 LRU 位置没跟着使用走，下次重建分片就会拿到新 jar"
        )
    finally:
        downloader.close()
