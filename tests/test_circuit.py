"""按域熔断。

设计要点在于**不误伤**：404 不算站点挂了、解析异常不算站点挂了、
代理坏了由重试吸收（所以计数发生在重试耗尽之后）。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from mineworker import setting
from mineworker.exceptions import (
    HttpStatusError,
    RequestError,
    SpiderError,
    ValidationError,
)
from mineworker.network import circuit, throttle
from mineworker.network.circuit import CircuitBreaker, counts_as_unhealthy
from mineworker.network.response import Response


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    circuit.reset()
    throttle.reset()
    monkeypatch.setattr(setting, "CIRCUIT_FAILURE_THRESHOLD", 3)
    monkeypatch.setattr(setting, "CIRCUIT_COOLDOWN", 30.0)
    monkeypatch.setattr(setting, "DOWNLOAD_DELAY", 0.0)
    monkeypatch.setattr(setting, "CONCURRENT_REQUESTS_PER_DOMAIN", 0)
    yield
    circuit.reset()
    throttle.reset()


def _resp(code: int) -> Response:
    return Response(url="https://a.com/x", status_code=code)


# ---- 哪些失败该算「站点不健康」-----------------------------------------
@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_unhealthy_status_codes_count(code: int) -> None:
    assert counts_as_unhealthy(None, _resp(code)) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 410])
def test_client_errors_do_not_count(code: int) -> None:
    """**这条是关键**：按 ID 顺序探测时连续几十个 404 很正常。

    拿它跳闸会把正常爬取搞瘫 —— 404 是「这个 URL 不行」，不是「这个站不行」。
    """
    assert counts_as_unhealthy(None, _resp(code)) is False


def test_network_error_counts() -> None:
    assert counts_as_unhealthy(RequestError("连不上"), None) is True


@pytest.mark.parametrize("exc", [ValidationError("校验失败"), SpiderError("解析炸了")])
def test_spider_side_errors_do_not_count(exc: Exception) -> None:
    """解析异常 / 校验失败是爬虫自己的问题，不该让目标站背锅。"""
    assert counts_as_unhealthy(exc, None) is False


def test_http_status_error_uses_its_code() -> None:
    assert counts_as_unhealthy(HttpStatusError(503, "https://a/"), None) is True
    assert counts_as_unhealthy(HttpStatusError(404, "https://a/"), None) is False


# ---- 跳闸逻辑 --------------------------------------------------------
def test_trips_at_threshold() -> None:
    cb = CircuitBreaker()
    assert cb.record_failure("https://a.com/1") is False
    assert cb.record_failure("https://a.com/2") is False
    assert cb.record_failure("https://a.com/3") is True, "第 3 次应跳闸（阈值 3）"


def test_success_resets_the_streak() -> None:
    """只认**连续**失败 —— 中间成功一次就重新计数。"""
    cb = CircuitBreaker()
    cb.record_failure("https://a.com/1")
    cb.record_failure("https://a.com/2")
    cb.record_success("https://a.com/ok")
    assert cb.failure_count("https://a.com/x") == 0
    assert cb.record_failure("https://a.com/3") is False


def test_domains_are_independent() -> None:
    cb = CircuitBreaker()
    cb.record_failure("https://a.com/1")
    cb.record_failure("https://a.com/2")
    assert cb.record_failure("https://b.com/1") is False
    assert cb.failure_count("https://b.com/x") == 1


def test_counter_clears_after_trip() -> None:
    """跳闸后清零：冷却结束就重新给站点机会，不必等它「证明恢复」。"""
    cb = CircuitBreaker()
    for i in range(3):
        cb.record_failure(f"https://a.com/{i}")
    assert cb.failure_count("https://a.com/x") == 0


def test_threshold_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "CIRCUIT_FAILURE_THRESHOLD", 0)
    cb = CircuitBreaker()
    for i in range(50):
        assert cb.record_failure(f"https://a.com/{i}") is False


# ---- 跳闸后复用 throttle 的整域冷却 ------------------------------------
def test_trip_penalizes_the_whole_domain() -> None:
    import time

    for i in range(3):
        circuit.record_failure(f"https://a.com/{i}")
    th = throttle._default
    with th._lock:
        scheduled = th._next_at.get("a.com", 0.0) - time.monotonic()
    assert scheduled > 25, "跳闸后该域应被挂上 ~30s 冷却"


def test_trip_does_not_affect_other_domains() -> None:
    import time

    for i in range(3):
        circuit.record_failure(f"https://a.com/{i}")
    t0 = time.monotonic()
    with throttle.slot("https://b.com/"):
        pass
    assert time.monotonic() - t0 < 0.1
