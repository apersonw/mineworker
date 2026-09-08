"""响应缓存（v4.10）。

写爬虫是来回试的过程：改一版选择器、重跑一遍看结果，一天下来同一批页面可能被
抓几十遍 —— 慢，而且对目标站不礼貌。打开缓存后第一遍照常抓，之后读本地文件。

**判据取自 HTTP 靶子的命中次数**：问框架「我用缓存了吗」是循环论证，
只有靶子知道请求到底有没有发出去。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request as WRequest
from werkzeug.wrappers import Response as WResponse

from mineworker import Request, setting
from mineworker.network import cache
from mineworker.network.downloader import close_default_downloaders


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(setting, "RESPONSE_CACHE_PATH", str(tmp_path / "cache"))
    monkeypatch.setattr(setting, "RESPONSE_CACHE_ENABLE", True)
    monkeypatch.setattr(setting, "RESPONSE_CACHE_EXPIRE", 3600.0)
    monkeypatch.setattr(setting, "ROBOTS_OBEY", False)
    yield
    close_default_downloaders()


def _counting(server: HTTPServer, path: str, body: str = "<h1>hi</h1>") -> list[str]:
    """靶子：每次被请求就记一笔。"""
    hits: list[str] = []

    def handler(request: WRequest) -> WResponse:
        hits.append(request.path)
        return WResponse(body, content_type="text/html")

    server.expect_request(path).respond_with_handler(handler)
    return hits


# ---- 核心：重跑不再打站点 --------------------------------------------
def test_second_request_does_not_hit_the_site(httpserver: HTTPServer) -> None:
    hits = _counting(httpserver, "/p")
    url = httpserver.url_for("/p")

    first = Request(url).download()
    second = Request(url).download()

    assert len(hits) == 1, f"第二次仍然打了站点（共 {len(hits)} 次）—— 缓存没生效"
    assert second.content == first.content
    assert second.status_code == first.status_code == 200
    assert second.css("h1::text").get() == "hi", "缓存回来的响应要能照常解析"


def test_disabled_by_default_means_every_run_hits_the_site(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认必须是关的 —— 生产上悄悄读几天前的副本是很坏的失败模式。"""
    monkeypatch.setattr(setting, "RESPONSE_CACHE_ENABLE", False)
    hits = _counting(httpserver, "/p")
    url = httpserver.url_for("/p")

    Request(url).download()
    Request(url).download()

    assert len(hits) == 2


def test_expired_entry_is_refetched(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = _counting(httpserver, "/p")
    url = httpserver.url_for("/p")

    Request(url).download()
    monkeypatch.setattr(setting, "RESPONSE_CACHE_EXPIRE", 0.0001)
    import time as _t

    _t.sleep(0.01)
    Request(url).download()

    assert len(hits) == 2, "过期了还在读缓存"


# ---- 什么不该缓存 ----------------------------------------------------
def test_post_is_never_cached(httpserver: HTTPServer) -> None:
    """POST 不幂等：把它的响应缓存下来重放，等于把一次写操作的结果当成了
    「这个请求现在会返回什么」——这两件事不是一回事。"""
    hits: list[str] = []

    def handler(request: WRequest) -> WResponse:
        hits.append(request.path)
        return WResponse("ok", content_type="text/plain")

    httpserver.expect_request("/submit", method="POST").respond_with_handler(handler)
    url = httpserver.url_for("/submit")

    Request(url, method="POST").download()
    Request(url, method="POST").download()

    assert len(hits) == 2, "POST 被缓存了"


def test_rate_limited_response_is_not_cached(httpserver: HTTPServer) -> None:
    """429 不能进缓存 —— 否则每次重跑都在读那张限速页，
    状态码策略还会以为站点一直在拒绝。"""
    hits: list[str] = []

    def handler(request: WRequest) -> WResponse:
        hits.append(request.path)
        return WResponse("slow down", status=429)

    httpserver.expect_request("/limited").respond_with_handler(handler)
    url = httpserver.url_for("/limited")

    Request(url).download()
    Request(url).download()

    assert len(hits) == 2, "429 被缓存了"


def test_different_urls_do_not_share_an_entry(httpserver: HTTPServer) -> None:
    a = _counting(httpserver, "/a", "<h1>A</h1>")
    b = _counting(httpserver, "/b", "<h1>B</h1>")

    ra = Request(httpserver.url_for("/a")).download()
    rb = Request(httpserver.url_for("/b")).download()

    assert ra.css("h1::text").get() == "A"
    assert rb.css("h1::text").get() == "B"
    assert len(a) == len(b) == 1


# ---- 缓存不能变成新的失败源 ------------------------------------------
def test_corrupt_entry_falls_back_to_a_real_request(httpserver: HTTPServer) -> None:
    """缓存文件坏了就当没有 —— 加速手段不该成为新的故障点。"""
    hits = _counting(httpserver, "/p")
    url = httpserver.url_for("/p")
    Request(url).download()

    for entry in Path(setting.RESPONSE_CACHE_PATH).rglob("*.json"):
        entry.write_text("{ 这不是合法 JSON", encoding="utf-8")

    resp = Request(url).download()

    assert len(hits) == 2, "读到坏缓存后没有回退到真实请求"
    assert resp.status_code == 200


def test_unwritable_cache_dir_does_not_break_crawling(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写不进去也不能让抓取失败 —— 那一步已经成功了。"""
    monkeypatch.setattr(setting, "RESPONSE_CACHE_PATH", "/proc/nonexistent/cache")
    _counting(httpserver, "/p")

    resp = Request(httpserver.url_for("/p")).download()

    assert resp.status_code == 200


def test_clear_removes_entries(httpserver: HTTPServer) -> None:
    _counting(httpserver, "/p")
    Request(httpserver.url_for("/p")).download()
    assert list(Path(setting.RESPONSE_CACHE_PATH).rglob("*.json"))

    removed = cache.clear()

    assert removed == 1
    assert not list(Path(setting.RESPONSE_CACHE_PATH).rglob("*.json"))


def test_cache_hit_does_not_consume_a_throttle_slot(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命中缓存不该被限速拖住 —— 否则「重跑不打扰目标站」只做了一半。"""
    import time as _t

    _counting(httpserver, "/p")
    url = httpserver.url_for("/p")
    Request(url).download()  # 先填缓存

    monkeypatch.setattr(setting, "DOWNLOAD_DELAY", 2.0)
    monkeypatch.setattr(setting, "RANDOMIZE_DOWNLOAD_DELAY", False)
    started = _t.monotonic()
    for _ in range(3):
        Request(url).download()
    elapsed = _t.monotonic() - started

    assert elapsed < 1.0, f"三次缓存命中花了 {elapsed:.1f}s —— 说明还在走限速排队"


def test_cached_response_keeps_cb_kwargs_reachable(httpserver: HTTPServer) -> None:
    """回调靠 `response.request` 拿 cb_kwargs，反序列化后必须补回来。"""
    _counting(httpserver, "/p")
    url = httpserver.url_for("/p")
    Request(url).download()

    req = Request(url, cb_kwargs={"page": 7})
    resp = req.download()

    assert resp.request is not None
    assert resp.request.cb_kwargs == {"page": 7}
