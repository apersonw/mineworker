from __future__ import annotations

import time

import httpx
import pytest
import respx

from mineworker import setting
from mineworker.utils.alert import (
    AlertManager,
    FeishuNotifier,
    LogNotifier,
    build_notifiers,
)
from mineworker.utils.stats import Stats


class Spy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send(self, title: str, message: str) -> None:
        self.calls.append((title, message))


@pytest.fixture(autouse=True)
def _fast_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "WARNING_INTERVAL", 0.0)
    monkeypatch.setattr(setting, "WARNING_STALL_SECONDS", 0.0)


def test_failed_rate_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "WARNING_MIN_REQUESTS", 4)
    monkeypatch.setattr(setting, "WARNING_FAILED_RATE", 0.5)
    stats = Stats()
    stats.incr("request_ok", 2)
    stats.incr("request_failed", 3)
    spy = Spy()
    AlertManager(stats, [spy]).check()
    assert any("失败率" in t for t, _ in spy.calls)


def test_no_alert_below_min_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "WARNING_MIN_REQUESTS", 100)
    monkeypatch.setattr(setting, "WARNING_FAILED_COUNT", 0)
    stats = Stats()
    stats.incr("request_failed", 5)
    spy = Spy()
    AlertManager(stats, [spy]).check()
    assert spy.calls == []


def test_failed_count_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "WARNING_FAILED_COUNT", 3)
    stats = Stats()
    stats.incr("request_failed", 3)
    spy = Spy()
    AlertManager(stats, [spy]).check()
    assert any("失败请求过多" in t for t, _ in spy.calls)


def test_stall_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "WARNING_STALL_SECONDS", 0.02)
    stats = Stats()
    stats.incr("request_ok", 1)
    mgr = AlertManager(stats, [Spy()])
    mgr.check()
    time.sleep(0.05)
    spy = Spy()
    mgr._notifiers = [spy]
    mgr.check()
    assert any("卡死" in t for t, _ in spy.calls)


def test_alert_dedup_within_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "WARNING_INTERVAL", 999.0)
    monkeypatch.setattr(setting, "WARNING_FAILED_COUNT", 1)
    stats = Stats()
    stats.incr("request_failed", 5)
    spy = Spy()
    mgr = AlertManager(stats, [spy])
    mgr.check()
    mgr.check()
    mgr.check()
    assert sum("失败请求过多" in t for t, _ in spy.calls) == 1


def test_first_alert_not_suppressed_by_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """「从没发过」必须能发 —— 否则刚启动的容器里第一条告警被静默吞掉。

    间隔取一个比任何 uptime 都大的值：`monotonic()` 原点是开机，一旦拿 0.0 当
    「从没发过」的哨兵，`now - 0.0 < WARNING_INTERVAL` 就恒真。开发机 uptime 动辄
    好几天，所以这个 bug 只在 CI / 新起的容器上现形。
    """
    monkeypatch.setattr(setting, "WARNING_INTERVAL", 1e12)
    monkeypatch.setattr(setting, "WARNING_FAILED_COUNT", 1)

    stats = Stats()
    stats.incr("request_failed", 5)
    spy = Spy()
    mgr = AlertManager(stats, [spy])
    mgr.check()

    assert sum("失败请求过多" in t for t, _ in spy.calls) == 1

    mgr.check()  # 同一间隔内不应再发
    assert sum("失败请求过多" in t for t, _ in spy.calls) == 1


def test_warning_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "WARNING_ENABLE", False)
    monkeypatch.setattr(setting, "WARNING_FAILED_COUNT", 1)
    stats = Stats()
    stats.incr("request_failed", 9)
    spy = Spy()
    AlertManager(stats, [spy]).check()
    assert spy.calls == []


@respx.mock
def test_feishu_notifier_posts_text() -> None:
    route = respx.post("https://feishu.test/hook").mock(return_value=httpx.Response(200))
    FeishuNotifier("https://feishu.test/hook").send("标题", "正文")
    assert route.called
    body = route.calls.last.request.content.decode("utf-8")
    assert "标题" in body
    assert "正文" in body


@respx.mock
def test_feishu_notifier_swallows_errors() -> None:
    respx.post("https://feishu.test/hook").mock(side_effect=httpx.ConnectError("x"))
    FeishuNotifier("https://feishu.test/hook").send("t", "m")  # 不抛


def test_build_notifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "WARNING_FEISHU_WEBHOOK", "https://x/y")
    monkeypatch.setattr(setting, "WARNING_EMAIL", {})
    notifiers = build_notifiers()
    assert any(isinstance(n, LogNotifier) for n in notifiers)
    assert any(isinstance(n, FeishuNotifier) for n in notifiers)


def test_notifier_exception_does_not_break_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setting, "WARNING_FAILED_COUNT", 1)
    stats = Stats()
    stats.incr("request_failed", 2)

    class Boom:
        def send(self, title: str, message: str) -> None:
            raise RuntimeError("boom")

    good = Spy()
    AlertManager(stats, [Boom(), good]).check()
    assert good.calls
