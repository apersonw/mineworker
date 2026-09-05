"""状态码策略与重试等待时长。

0.7.0 的破坏性变更：此前 429 / 503 / 404 的响应体直接进 parse() 被当成数据。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from mineworker import Request, setting
from mineworker.exceptions import HttpStatusError, RequestError
from mineworker.network import status
from mineworker.network.response import Response


def _resp(code: int, headers: dict[str, str] | None = None) -> Response:
    return Response(url="https://x/", status_code=code, headers=headers or {})


# ---- 分类 ------------------------------------------------------------
@pytest.mark.parametrize("code", [200, 201, 204, 299])
def test_2xx_is_ok(code: int) -> None:
    assert status.classify(_resp(code)) == "ok"


@pytest.mark.parametrize("code", [301, 302, 307, 399])
def test_3xx_is_ok(code: int) -> None:
    """3xx 能走到回调说明用户显式关了 allow_redirects —— 那就是他要的结果。

    归为 fail 会打断「手工处理重定向」这种正当用法。
    """
    assert status.classify(_resp(code)) == "ok"


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_retryable_codes(code: int) -> None:
    assert status.classify(_resp(code)) == "retry"


@pytest.mark.parametrize("code", [400, 401, 403, 404, 410, 418])
def test_other_non_2xx_is_fail(code: int) -> None:
    assert status.classify(_resp(code)) == "fail"


def test_accept_status_codes_opt_back_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "ACCEPT_STATUS_CODES", [404])
    assert status.classify(_resp(404)) == "ok"
    assert status.classify(_resp(403)) == "fail"


def test_check_disabled_restores_old_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    """CHECK_STATUS_CODE=False 必须完全退回 0.6.0 的「全部放行」。"""
    monkeypatch.setattr(setting, "CHECK_STATUS_CODE", False)
    for code in (404, 429, 500, 503):
        assert status.classify(_resp(code)) == "ok"


# ---- Retry-After 解析 ------------------------------------------------
def test_retry_after_seconds_format() -> None:
    assert status.parse_retry_after("120", now=0.0) == 120.0


def test_retry_after_http_date_format() -> None:
    now = time.time()
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    got = status.parse_retry_after(format_datetime(when), now=now)
    assert got is not None and 25 <= got <= 35


def test_retry_after_past_date_clamps_to_zero() -> None:
    when = datetime.now(timezone.utc) - timedelta(hours=1)
    assert status.parse_retry_after(format_datetime(when), now=time.time()) == 0.0


@pytest.mark.parametrize("raw", ["", "  ", "soon", "not-a-date"])
def test_retry_after_garbage_returns_none(raw: str) -> None:
    assert status.parse_retry_after(raw, now=0.0) is None


def test_retry_after_only_for_429_and_503() -> None:
    hdr = {"retry-after": "30"}
    assert status.retry_after_seconds(_resp(429, hdr), now=0.0) == 30.0
    assert status.retry_after_seconds(_resp(503, hdr), now=0.0) == 30.0
    # 500 没有 Retry-After 语义，别乱读
    assert status.retry_after_seconds(_resp(500, hdr), now=0.0) is None


# ---- 等待时长的优先级 ------------------------------------------------
def test_retry_after_beats_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务端明说了等多久就听它的，不要用自己的退避覆盖。"""
    monkeypatch.setattr(setting, "RETRY_BACKOFF", 10.0)
    req = Request("https://x/")
    req.retry_times = 3
    assert status.retry_delay(req, _resp(429, {"retry-after": "7"}), now=0.0) == 7.0


def test_exponential_backoff_with_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "RETRY_BACKOFF", 2.0)
    monkeypatch.setattr(setting, "RETRY_AFTER_MAX", 600.0)
    req = Request("https://x/")
    req.retry_times = 3  # 2 * 2**2 = 8，抖动后落在 [4, 8]
    delay = status.retry_delay(req, _resp(500), now=0.0)
    assert 4.0 <= delay <= 8.0


def test_backoff_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "RETRY_BACKOFF", 1.0)
    monkeypatch.setattr(setting, "RETRY_AFTER_MAX", 5.0)
    req = Request("https://x/")
    req.retry_times = 20  # 不封顶的话是天文数字
    assert status.retry_delay(req, _resp(500), now=0.0) <= 5.0


def test_falls_back_to_spider_retry_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 Retry-After 也没开退避时，行为与 0.6.0 一致。"""
    monkeypatch.setattr(setting, "RETRY_BACKOFF", 0.0)
    monkeypatch.setattr(setting, "SPIDER_RETRY_INTERVAL", 1.5)
    assert status.retry_delay(Request("https://x/"), _resp(500), now=0.0) == 1.5


def test_retry_after_too_long_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """等十分钟不值得占着 worker —— 该判失败让位。"""
    monkeypatch.setattr(setting, "RETRY_AFTER_MAX", 60.0)
    assert status.retry_after_too_long(_resp(429, {"retry-after": "600"}), now=0.0) == 600.0
    assert status.retry_after_too_long(_resp(429, {"retry-after": "10"}), now=0.0) is None


def test_retry_after_max_zero_disables_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "RETRY_AFTER_MAX", 0.0)
    assert status.retry_after_seconds(_resp(429, {"retry-after": "30"}), now=0.0) is None


# ---- 异常类型 --------------------------------------------------------
def test_http_status_error_is_request_error() -> None:
    """继承关系是设计要点：复用既有的 except RequestError 与 retry 回放。"""
    exc = HttpStatusError(404, "https://x/")
    assert isinstance(exc, RequestError)
    assert exc.status_code == 404
    assert "404" in str(exc)
