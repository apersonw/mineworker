"""curl 的 session 也要按线程分片 —— 但它买到的**不是**连接复用。

这两件事必须分开说，因为我先前把它们混成一句话写错了两遍。

**一、curl 这条路径从来没有连接复用。**
框架的 curl 下载器永远用 `stream=True`（为了 `MAX_RESPONSE_SIZE` 边读边判），
而 curl_cffi 的 `Response._finalize_stream()` 收尾时执行 `self.curl.close()` ——
关掉的是**整个 Curl 句柄**，句柄的连接缓存随之消失，不是把连接归还池子。
实测（同线程 5 次串行、数保活 socket）：

    impersonate=无   · stream=False → 2 条    stream=True → 0 条
    impersonate=chrome · stream=False → 2 条    stream=True → 0 条
    对照 httpx.Client                          → 2 条

所以 `use_session=True` 在 curl 这边**只带来 cookie 持久化**。
先前的注释和文档写的是「三个下载器都复用连接」，那句话对 curl 是错的。

**二、共用一个 Session 仍然要付代价，而且拐点与 httpx 不同。**
curl 从 **16** 线程起就不再增长，httpx 是 32 —— 所以有独立的
`CURL_SESSION_SHARD_THREADS`。复用同一个数字的话，32 线程那格
`ceil(32/32)=1` 等于没分片（实测 230 QPS，分片后 391）。
即便收益为零，共用的代价是实打实的（50ms 靶子、stream=True，与框架一致）：

    线程            8    16    32    48    64
    共用一个      133   235   238   237   242    ← 16 线程起就不再增长
    每 16 线程一个 132   238   391   517   589
    每线程一个    133   237   392   485   596

64 线程 2.4×，而「每 16 线程一个」已追平「每线程一个」，不必一线程一个。
curl_cffi 自己的 Session 文档也写着「建议每个线程一个 session」。
"""

from __future__ import annotations

import threading
from http.cookiejar import CookieJar

import pytest

from mineworker import setting
from mineworker.network.downloader._curl import CurlDownloader

pytest.importorskip("curl_cffi")


def test_at_or_below_the_knee_stays_single_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """线程数不超过一片的容量时只有一个 session —— 对现有部署零影响。"""
    monkeypatch.setattr(setting, "CURL_SESSION_SHARD_THREADS", 32)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 4)
    dl = CurlDownloader(use_session=True, impersonate="chrome")
    try:
        assert dl._session_for_proxy("http://p:1") is dl._session_for_proxy("http://p:1")
        assert len(dl._sessions) == 1
    finally:
        dl.close()


def test_threads_land_on_different_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """多个线程要真的落到不同的 session 上，否则分了等于没分。"""
    monkeypatch.setattr(setting, "CURL_SESSION_SHARD_THREADS", 4)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 16)  # → 4 片
    dl = CurlDownloader(use_session=True, impersonate="chrome")
    seen: set[int] = set()
    lock = threading.Lock()

    def worker() -> None:
        s = dl._session_for_proxy("http://p:1")
        with lock:
            seen.add(id(s))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(seen) > 1, "16 个线程全挤在同一个 session 上，分片没生效"
        assert len(seen) <= 4
    finally:
        dl.close()


def test_all_shards_share_one_cookie_jar(monkeypatch: pytest.MonkeyPatch) -> None:
    """**核心用例**：分片不能把 cookie 切开。

    `use_session` 在 curl 这边的唯一价值就是 cookie 持久化 ——
    分片若把它切开，这个开关就彻底没用了。
    """
    monkeypatch.setattr(setting, "CURL_SESSION_SHARD_THREADS", 1)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 4)  # → 4 片
    dl = CurlDownloader(use_session=True, impersonate="chrome")
    try:
        got: list[object] = []
        barrier = threading.Barrier(4)
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            s = dl._session_for_proxy("http://p:1")
            with lock:
                got.append(s)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len({id(s) for s in got}) > 1, "没分出多片，这条用例就没验到东西"
        got[0].cookies.set("sid", "abc", domain="example.com")  # type: ignore[attr-defined]
        for s in got[1:]:
            assert s.cookies.get("sid", domain="example.com") == "abc", (  # type: ignore[attr-defined]
                "分片之间 cookie 不通 —— use_session 在 curl 这边就只剩这一个作用了"
            )
    finally:
        dl.close()


def test_jar_is_per_proxy_not_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """不同代理之间 cookie 仍然隔离 —— 原本就有的行为，别顺手改掉。"""
    monkeypatch.setattr(setting, "CURL_SESSION_SHARD_THREADS", 32)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 4)
    dl = CurlDownloader(use_session=True, impersonate="chrome")
    try:
        a = dl._session_for_proxy("http://p:1")
        b = dl._session_for_proxy("http://p:2")
        a.cookies.set("sid", "abc", domain="example.com")
        assert b.cookies.get("sid", domain="example.com") is None
    finally:
        dl.close()


def test_curl_cffi_still_takes_a_raw_jar_by_reference() -> None:
    """钉住这次修复依赖的 curl_cffi 行为。

    `Cookies.__init__` 对**裸 CookieJar** 走 `else: self.jar = cookies`，即按引用。
    哪天它改成拷贝，分片就会静默地把 cookie 切开 —— 这条要在那时立刻红，
    而不是等线上登录态莫名丢失。
    """
    from curl_cffi import requests as cr

    jar = CookieJar()
    s1 = cr.Session(cookies=jar)
    s2 = cr.Session(cookies=jar)
    try:
        assert s1.cookies.jar is jar
        s1.cookies.set("k", "v", domain="example.com")
        assert s2.cookies.get("k", domain="example.com") == "v"
    finally:
        s1.close()
        s2.close()
