"""指标上报：定时打印一行进度，并可选把关键指标推给 Prometheus。

Prometheus 需 ``pip install mineworker[metrics]`` 且设 ``METRICS_PROMETHEUS_PORT > 0``。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from mineworker import setting
from mineworker.utils import stats as sk
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.utils.stats import Stats

log = get_logger("metrics")

#: name -> 取当前值的函数（队列深度、在途数等）
Probes = dict[str, Callable[[], int]]


class _Prometheus:
    """把 Stats 的累计值与实时探针都映射成 Gauge（本地是唯一真相来源）。"""

    def __init__(self, port: int) -> None:
        from prometheus_client import CollectorRegistry, Gauge, start_http_server

        self._registry = CollectorRegistry()
        self._gauge_cls = Gauge
        self._gauges: dict[str, object] = {}
        start_http_server(port, registry=self._registry)
        log.info("Prometheus exporter 已启动 :{}/metrics", port)

    def update(self, values: dict[str, int]) -> None:
        for key, value in values.items():
            gauge = self._gauges.get(key)
            if gauge is None:
                gauge = self._gauge_cls(f"mineworker_{key}", key, registry=self._registry)
                self._gauges[key] = gauge
            gauge.set(value)  # type: ignore[attr-defined]


def _make_prometheus(port: int) -> _Prometheus | None:
    if port <= 0:
        return None
    try:
        return _Prometheus(port)
    except ImportError:
        log.warning("未安装 prometheus-client，跳过 exporter（pip install mineworker[metrics]）")
        return None
    except OSError as exc:
        log.warning("Prometheus exporter 启动失败：{!r}", exc)
        return None


class MetricsReporter(threading.Thread):
    def __init__(self, stats: Stats, probes: Probes) -> None:
        super().__init__(name="metrics", daemon=True)
        self._stats = stats
        self._probes = probes
        self._stop_event = threading.Event()
        self._prom = _make_prometheus(setting.METRICS_PROMETHEUS_PORT)
        self._last_ok = 0
        self._last_ts = time.monotonic()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        interval = max(1.0, setting.METRICS_LOG_INTERVAL)
        while not self._stop_event.wait(interval):
            self._tick()

    def _tick(self) -> None:
        counters = self._stats.as_dict()
        gauges = {name: probe() for name, probe in self._probes.items()}
        now = time.monotonic()
        ok = counters.get(sk.REQUEST_OK, 0)
        rate = (ok - self._last_ok) / (now - self._last_ts) if now > self._last_ts else 0.0
        self._last_ok, self._last_ts = ok, now

        log.info(
            "进度 | 成功 {} 失败 {} 重试 {} | 队列 {} 在途 {} | 入库 {} | {:.1f} 请求/s",
            ok,
            counters.get(sk.REQUEST_FAILED, 0),
            counters.get(sk.RETRY, 0),
            gauges.get("queue_depth", 0),
            gauges.get("in_flight", 0),
            counters.get(sk.ITEM, 0),
            rate,
        )
        if self._prom is not None:
            self._prom.update({**counters, **gauges})
