"""爬虫结束时吐一行**机器可读**的摘要，供 NetspyHub 读容器日志用。

**为什么要它。** 生产复盘（见 test_run_scope）里，Hub 判成败只看 exit code，
1158 个空转实例全被记成 success —— 它没有任何办法从日志里读出「请求成功 0」。
那行「爬虫结束 | 请求成功 63 …」是给人看的：带 loguru 的时间戳、颜色、中文，
还受 LOG_LEVEL 摆布（有人跑 WARNING / CRITICAL，那行就没了）。

所以另出一行契约行：稳定前缀 + 一行紧凑 JSON，字段名固定，**不受 LOG_LEVEL
影响**（Hub 要能无条件读到它，否则又回到「跑了 ≠ 抓了」分不清的老问题）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import fakeredis
import pytest
from pytest_httpserver import HTTPServer

from netspy import Item, Request, Spider, setting
from netspy.core import redis_scheduler
from netspy.pipelines.base import BasePipeline
from netspy.utils.stats import RUN_SUMMARY_MARKER


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_scheduler, "get_redis", lambda url=None: client)
    from netspy.dedup import redis_filter

    monkeypatch.setattr(redis_filter, "_default_redis", lambda: client)
    yield client
    client.flushall()


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "DONE_CHECK_INTERVAL", 0.05)
    monkeypatch.setattr(setting, "DONE_CHECK_TIMES", 2)
    monkeypatch.setattr(setting, "BUFFER_FLUSH_INTERVAL", 0.02)
    monkeypatch.setattr(setting, "HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr(setting, "HEARTBEAT_STALE", 5.0)
    monkeypatch.setattr(setting, "RANDOM_USER_AGENT", False)
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 2)
    monkeypatch.setattr(setting, "SPIDER_STARTUP_GRACE", 0.2)
    monkeypatch.setattr(setting, "DEDUP_FILTER", "redis")


PAGES = 3


def _serve(server: HTTPServer) -> None:
    from werkzeug import Response

    def seed(_: Any) -> Any:
        links = "".join(f'<a href="/p/{i}">p{i}</a>' for i in range(PAGES))
        return Response(f"<html><body>{links}</body></html>", content_type="text/html")

    def page(_: Any) -> Any:
        return Response("<html><body><h1>ok</h1></body></html>", content_type="text/html")

    server.expect_request("/seed").respond_with_handler(seed)
    for i in range(PAGES):
        server.expect_request(f"/p/{i}").respond_with_handler(page)


class Book(Item):
    __table_name__ = "books"
    __unique_key__ = ["url"]

    url: str


class Sink(BasePipeline):
    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        return True


class Crawl(Spider):
    def __init__(self, seed_url: str, **kw: Any) -> None:
        self._seed_url = seed_url
        super().__init__(pipelines=[f"{__name__}.Sink"], **kw)

    def start_requests(self) -> Iterator[Request]:
        yield Request(self._seed_url, callback=self.parse_seed)

    def parse_seed(self, request: Request, response: Any) -> Iterator[Any]:
        for href in response.css("a::attr(href)").getall():
            yield Request(response.urljoin(href), callback=self.parse_page)

    def parse_page(self, request: Request, response: Any) -> Iterator[Any]:
        item = Book()
        item.url = response.url
        yield item


def _summary_lines(captured: str) -> list[dict[str, Any]]:
    """从 stdout 里挑出契约行，解析其后的 JSON。

    Hub 就是这么做的：按前缀 grep，取前缀之后那一段当 JSON。
    """
    out = []
    for line in captured.splitlines():
        idx = line.find(RUN_SUMMARY_MARKER)
        if idx != -1:
            out.append(json.loads(line[idx + len(RUN_SUMMARY_MARKER) :]))
    return out


# ======================================================================
def test_summary_is_one_line_of_valid_json_with_stable_keys(
    httpserver: HTTPServer, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(httpserver)
    Crawl(httpserver.url_for("/seed"), redis_key="sum1").start()

    summaries = _summary_lines(capsys.readouterr().out)
    assert len(summaries) == 1, "结束时应恰好吐一行摘要"
    s = summaries[0]
    # Hub 会硬编码这些键 —— 少一个都是破坏性变更
    for key in (
        "schema",
        "request_ok",
        "request_failed",
        "items",
        "item_dedup_dropped",
        "item_failed",
        "dedup_dropped",
        "parse_error",
        "retry",
        "dropped",
        "elapsed",
        "spider",
    ):
        assert key in s, f"摘要缺字段 {key}：{s}"
    assert s["request_ok"] == PAGES + 1
    assert s["items"] == PAGES
    assert s["spider"] == "Crawl"


def test_summary_survives_a_quiet_log_level(
    httpserver: HTTPServer, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**这一条是重点**：LOG_LEVEL 调到 CRITICAL，人看的那行没了，机器这行必须还在。

    生产 worker 里真有人把日志压到 WARNING。契约行受 LOG_LEVEL 摆布的话，
    Hub 就又什么都读不到了。
    """
    from netspy.utils import log

    monkeypatch.setattr(setting, "LOG_LEVEL", "CRITICAL")
    log.configure()
    try:
        _serve(httpserver)
        Crawl(httpserver.url_for("/seed"), redis_key="sum2").start()
        captured = capsys.readouterr().out
    finally:
        monkeypatch.setattr(setting, "LOG_LEVEL", "INFO")
        log.configure()

    summaries = _summary_lines(captured)
    assert len(summaries) == 1, "LOG_LEVEL=CRITICAL 时机器摘要不该消失"
    assert summaries[0]["request_ok"] == PAGES + 1


def test_summary_makes_an_empty_run_machine_detectable(
    httpserver: HTTPServer, capsys: pytest.CaptureFixture[str]
) -> None:
    """空转的实例，Hub 能一眼认出：request_ok=0 且 items=0，而 exit code 仍是 0。

    这正是「成功但 0 请求」那条告警规则要读的两个数。
    """
    _serve(httpserver)
    # 第一轮真抓，第二轮同 redis_key、无 RUN_ID → 空转（见 test_run_scope）
    Crawl(httpserver.url_for("/seed"), redis_key="sum3").start()
    capsys.readouterr()
    Crawl(httpserver.url_for("/seed"), redis_key="sum3").start()

    s = _summary_lines(capsys.readouterr().out)[-1]
    assert s["request_ok"] == 0 and s["items"] == 0


def test_summary_carries_run_context_when_scoped(
    httpserver: HTTPServer, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """设了 RUN_ID 时摘要要带上它 —— Hub 靠它把这行日志关联到具体实例/运行。"""
    _serve(httpserver)
    monkeypatch.setattr(setting, "RUN_ID", "run-42")
    Crawl(httpserver.url_for("/seed"), redis_key="sum4").start()

    s = _summary_lines(capsys.readouterr().out)[-1]
    assert s["run_id"] == "run-42"
    assert s["namespace"] == "netspy:sum4:run:run-42"


def test_summary_run_id_is_empty_for_single_machine(
    httpserver: HTTPServer, capsys: pytest.CaptureFixture[str]
) -> None:
    """没有运行作用域时 run_id 是空串（不是缺字段）—— Hub 不必分两种形状解析。"""
    _serve(httpserver)
    Crawl(httpserver.url_for("/seed"), redis_key="sum5").start()

    s = _summary_lines(capsys.readouterr().out)[-1]
    assert s["run_id"] == ""


def test_summary_can_be_turned_off(
    httpserver: HTTPServer, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(httpserver)
    monkeypatch.setattr(setting, "RUN_SUMMARY_ENABLE", False)
    Crawl(httpserver.url_for("/seed"), redis_key="sum6").start()
    assert _summary_lines(capsys.readouterr().out) == []
