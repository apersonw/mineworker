"""HTTP 状态码策略与重试等待时长。

0.7.0 之前框架不检查状态码：`validate()` 默认返回 True，于是 429 / 503 / 404 的
响应体会直接进 `parse()` 被当成数据 —— 被限速时不但不退避，还会把限速提示页入库。

这里的函数都是纯函数，便于单测；接入点在 `core/parser_control.py`。
"""

from __future__ import annotations

import random
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Literal

from mineworker import setting

if TYPE_CHECKING:
    from mineworker.network.request import Request
    from mineworker.network.response import Response

Verdict = Literal["ok", "retry", "fail"]

#: 带 Retry-After 语义的状态码（RFC 9110）
_RETRY_AFTER_CODES = frozenset({429, 503})


def classify(response: Response) -> Verdict:
    """判定一个响应该放行、重试还是判失败。"""
    if not setting.CHECK_STATUS_CODE:
        return "ok"
    code = response.status_code
    # 3xx 能走到这里说明用户显式关了 allow_redirects —— 那就是他要的结果。
    # 归为 ok 才不会打断「手工处理重定向」这种正当用法。
    if 200 <= code < 400:
        return "ok"
    if code in setting.ACCEPT_STATUS_CODES:
        return "ok"
    if code in setting.RETRY_STATUS_CODES:
        return "retry"
    return "fail"


def parse_retry_after(value: str, *, now: float) -> float | None:
    """解析 ``Retry-After``：既支持秒数，也支持 HTTP-date。无法解析返回 None。"""
    value = value.strip()
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(when.timestamp() - now, 0.0)


def retry_after_seconds(response: Response | None, *, now: float) -> float | None:
    """仅对 429 / 503 读取 ``Retry-After``。"""
    if response is None or not setting.RETRY_AFTER_MAX:
        return None
    if response.status_code not in _RETRY_AFTER_CODES:
        return None
    raw = response.headers.get("retry-after")
    return parse_retry_after(raw, now=now) if raw else None


def retry_delay(request: Request, response: Response | None, *, now: float) -> float:
    """本次重试前该等多久。**唯一的等待时长计算入口**，优先级固定：

    1. ``Retry-After`` 头（仅 429 / 503）—— 服务端明说了等多久，就听它的
    2. ``RETRY_BACKOFF > 0`` → 指数退避 + 抖动（抖动是为了避免多个 worker 同步重试）
    3. 否则沿用 ``SPIDER_RETRY_INTERVAL``（默认 0.0，即 0.6.0 的行为）
    """
    explicit = retry_after_seconds(response, now=now)
    if explicit is not None:
        return explicit
    backoff = setting.RETRY_BACKOFF
    if backoff > 0:
        raw = backoff * (2 ** max(request.retry_times - 1, 0))
        capped = min(raw, setting.RETRY_AFTER_MAX) if setting.RETRY_AFTER_MAX else raw
        return float(capped * random.uniform(0.5, 1.0))
    return float(setting.SPIDER_RETRY_INTERVAL)


def retry_after_too_long(response: Response | None, *, now: float) -> float | None:
    """``Retry-After`` 超过 ``RETRY_AFTER_MAX`` 时返回它的秒数，否则 None。

    等十分钟没有意义 —— 与其占着一个 worker 干等，不如判失败让位给别的任务。
    """
    seconds = retry_after_seconds(response, now=now)
    if seconds is None or not setting.RETRY_AFTER_MAX:
        return None
    return seconds if seconds > setting.RETRY_AFTER_MAX else None
