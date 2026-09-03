from __future__ import annotations

import socket

import httpx
import pytest

from mineworker import setting
from mineworker.utils.metrics import MetricsReporter
from mineworker.utils.stats import Stats


def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def test_tick_computes_rate_without_error() -> None:
    stats = Stats()
    stats.incr("request_ok", 10)
    reporter = MetricsReporter(stats, {"queue_depth": lambda: 3, "in_flight": lambda: 1})
    reporter._tick()
    assert reporter._last_ok == 10
    stats.incr("request_ok", 5)
    reporter._tick()
    assert reporter._last_ok == 15


def test_prometheus_disabled_when_port_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "METRICS_PROMETHEUS_PORT", 0)
    assert MetricsReporter(Stats(), {})._prom is None


def test_prometheus_exporter_serves_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _free_port()
    monkeypatch.setattr(setting, "METRICS_PROMETHEUS_PORT", port)
    stats = Stats()
    stats.incr("request_ok", 7)
    stats.incr("item", 2)
    reporter = MetricsReporter(stats, {"queue_depth": lambda: 4})
    assert reporter._prom is not None

    reporter._tick()
    body = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5).text
    assert "mineworker_request_ok 7.0" in body
    assert "mineworker_item 2.0" in body
    assert "mineworker_queue_depth 4.0" in body


def test_reporter_thread_starts_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "METRICS_LOG_INTERVAL", 0.02)
    reporter = MetricsReporter(Stats(), {"queue_depth": lambda: 0})
    reporter.start()
    reporter.stop()
    reporter.join(timeout=3)
    assert not reporter.is_alive()
