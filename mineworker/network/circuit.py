"""按域熔断：目标站挂了就别再死磕。

连续失败到阈值时给该域挂一段冷却，复用
:func:`~mineworker.network.throttle.penalize` 的整域降速机制 ——
所有工作线程一起避开，不需要另起一套等待通道。

**只数「站点不健康」的信号**：网络错误、5xx、429。404 等 4xx 一律不计 ——
「按 ID 顺序探测」是常见爬法，连续十几个 404 很正常，
拿它跳闸会把正常爬取搞瘫。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from mineworker import setting
from mineworker.exceptions import HttpStatusError, RequestError, ResponseTooLargeError
from mineworker.network import throttle
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.network.response import Response

log = get_logger("circuit")

#: 计入熔断的状态码：服务端自己出问题或明确要求退避
_UNHEALTHY_CODES = frozenset({429, 500, 502, 503, 504})


def counts_as_unhealthy(exc: BaseException | None, response: Response | None) -> bool:
    """这次失败是否说明「站点不健康」。

    解析异常、校验失败是**爬虫自己的问题**，不该让目标站背锅；
    404 / 403 等是「这个 URL 不行」，不是「这个站不行」。
    """
    if response is not None:
        return response.status_code in _UNHEALTHY_CODES
    if isinstance(exc, HttpStatusError):
        return exc.status_code in _UNHEALTHY_CODES
    # 响应体超限是「这个 URL 太大」，不是「这个站挂了」—— 一个站上有几个
    # 大 PDF 就把整域熔断，那是把礼貌性机制变成了自伤
    if isinstance(exc, ResponseTooLargeError):
        return False
    # 网络层错误（超时、连不上、TLS 失败……）
    return isinstance(exc, RequestError)


class CircuitBreaker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}

    def record_success(self, url: str) -> None:
        domain = throttle.domain_of(url)
        with self._lock:
            if domain in self._failures:
                del self._failures[domain]

    def record_failure(self, url: str) -> bool:
        """记一次失败；返回本次是否刚好跳闸。"""
        threshold = setting.CIRCUIT_FAILURE_THRESHOLD
        if threshold <= 0:
            return False
        domain = throttle.domain_of(url)
        with self._lock:
            count = self._failures.get(domain, 0) + 1
            self._failures[domain] = count
            tripped = count >= threshold
            if tripped:
                # 清零：冷却结束后重新给站点一次机会，不必等它「恢复」
                del self._failures[domain]
        if tripped:
            cooldown = setting.CIRCUIT_COOLDOWN
            log.warning(
                "{} 连续失败 {} 次，熔断 {:.0f}s（该域所有请求一起避让）",
                domain,
                threshold,
                cooldown,
            )
            throttle.penalize(url, cooldown)
        return tripped

    def failure_count(self, url: str) -> int:
        with self._lock:
            return self._failures.get(throttle.domain_of(url), 0)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


#: 进程级单例
_default = CircuitBreaker()


def record_success(url: str) -> None:
    _default.record_success(url)


def record_failure(url: str) -> bool:
    return _default.record_failure(url)


def failure_count(url: str) -> int:
    return _default.failure_count(url)


def reset() -> None:
    _default.reset()
