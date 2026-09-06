"""benchmark harness 的防腐测试 —— 只保证它还能跑，不测性能。

性能数字有方差，放进 CI 只会制造 flaky。这里用极小规模验证：
靶子起得来、指标算得对、跑一格不炸。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

from run import _run_once
from server import BenchServer, _raw_hammer, make_body


def test_server_serves_and_counts() -> None:
    with BenchServer(latency=0.0, n_items=5) as srv:
        asyncio.run(_raw_hammer(srv.host, srv.port, n_conn=4, per_conn=5))
        s = srv.stats
        assert s.total == 20
        assert s.max_inflight >= 1
        assert s.qps > 0


def test_time_weighted_average_never_exceeds_peak() -> None:
    """平均在途按定义不可能超过峰值 —— 这条不变式挂了说明积分算错了。"""
    with BenchServer(latency=0.01) as srv:
        asyncio.run(_raw_hammer(srv.host, srv.port, n_conn=8, per_conn=3))
        s = srv.stats
        assert 0 < s.avg_inflight <= s.max_inflight


def test_body_is_parseable_html() -> None:
    from parsel import Selector

    sel = Selector(make_body(7).decode())
    assert len(sel.css("li.item")) == 7
    assert sel.css("li.item a::text").get() == "条目 0"


@pytest.mark.parametrize("downloader", ["sync", "async"])
def test_run_once_produces_sane_metrics(downloader: str) -> None:
    r = _run_once(threads=2, downloader=downloader, session=True, latency=0.0, parse="heavy", n=12)
    assert r.qps > 0
    assert r.peak_inflight >= 1
    assert 0 < r.avg_inflight <= r.peak_inflight
    assert r.wall > 0


# ---- soak 画像的防腐 --------------------------------------------------
def test_rss_probe_returns_a_number() -> None:
    """RSS 探测必须在精简镜像里也能用。

    最初只 shell 到 `ps`，而 python:3.12-slim 没装它（procps 包）——
    在 Linux 容器里直接 FileNotFoundError。现在优先读 /proc/self/statm。
    """
    from soak import rss_mb

    value = rss_mb()
    assert value == value, "rss_mb() 返回 NaN —— RSS 探测在本平台不可用"
    assert value > 0


def test_one_shot_runs_and_reports() -> None:
    """soak 的单轮能跑通并打出一行 markdown。"""
    import io
    from contextlib import redirect_stdout

    from soak import _one_shot

    buf = io.StringIO()
    with redirect_stdout(buf):
        _one_shot(threads=2, n=10)
    line = buf.getvalue().strip()
    assert line.startswith("| 2 |") and line.endswith("|"), line
