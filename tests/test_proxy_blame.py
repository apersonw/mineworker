"""不是所有失败都算代理的错。

原先下载器里任何 `httpx.HTTPError` 都会 `report_bad_proxy`，不区分
「连不上代理」和「目标站自己慢」。实测后果：三个**完全健康**的代理
（各自都成功连上并转发了请求）+ 一个 5 秒才回的目标站 + 1 秒超时 ——
**三个请求就把整池清空**，之后每个请求都要等满 `PROXY_WAIT_TIMEOUT`
（默认 30 秒）才失败。一个慢 URL 让整个爬虫停摆一分钟。

判据分两半，缺一不可：
  · 目标站的锅**不该**拉黑代理；
  · 代理真的连不上时**仍然**要立刻拉黑（否则等于把健康检查关了）。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mineworker import setting
from mineworker.network.downloader._common import is_proxy_fault
from mineworker.network.proxy_pool.api import ApiProxyPool

_LIST = "https://p/list"
_P = "1.1.1.1:80"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr("mineworker.network.proxy_pool.api.time.monotonic", lambda: now[0])
    return now


def _pool(text: str = _P) -> ApiProxyPool:
    respx.get(_LIST).mock(return_value=httpx.Response(200, text=text))
    return ApiProxyPool(_LIST)


# ---- 谁的错：分类本身 ---------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "blamed"),
    [
        (httpx.ConnectError("拨不通"), True),
        (httpx.ConnectTimeout("拨号超时"), True),
        (httpx.ProxyError("CONNECT 被拒"), True),
        (httpx.ReadTimeout("目标站太慢"), False),
        (httpx.ReadError("读到一半断了"), False),
        (httpx.RemoteProtocolError("响应畸形"), False),
        (httpx.PoolTimeout("本地池排队超时"), False),
    ],
)
def test_only_connect_phase_failures_are_the_proxys_fault(exc: Exception, blamed: bool) -> None:
    """配了代理时，TCP 连接的对端就是代理 —— 连不上只可能是它的问题。
    一旦连上了，后面的失败说不清是谁的错。
    """
    assert is_proxy_fault(exc) is blamed


def test_curl_error_codes_are_classified_too() -> None:
    """curl 那条路径报的是 CURLE_* 码，不是 httpx 异常。"""

    class _CurlError(Exception):
        def __init__(self, code: int) -> None:
            self.code = code

    assert is_proxy_fault(_CurlError(7)) is True  # COULDNT_CONNECT
    assert is_proxy_fault(_CurlError(97)) is True  # PROXY
    assert is_proxy_fault(_CurlError(28)) is False  # OPERATION_TIMEDOUT


# ---- 池的行为 -----------------------------------------------------------


@respx.mock
def test_dead_proxy_is_still_banned_immediately(clock: list[float]) -> None:
    """**阳性对照**：代理真的连不上时，第一次就拉黑。

    少了这一条，「别拉黑」的修法可以退化成「永不拉黑」而全部用例照绿。
    """
    pool = _pool()
    pool.report_bad(f"http://{_P}")
    assert pool.get_proxy() is None, "连不上的代理居然还在发"


@respx.mock
def test_target_side_failure_does_not_ban_on_the_first_try(clock: list[float]) -> None:
    """目标站慢一次，代理不该有事。"""
    pool = _pool()
    pool.report_suspect(f"http://{_P}")
    assert pool.get_proxy() == f"http://{_P}"


@respx.mock
def test_suspects_still_ban_once_they_pile_up(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """真挂掉的代理会连续失败 —— 攒够阈值照样拉黑，只是晚几次。"""
    monkeypatch.setattr(setting, "PROXY_SUSPECT_BAN_AFTER", 3)
    pool = _pool()
    for _ in range(3):
        pool.report_suspect(f"http://{_P}")
    assert pool.get_proxy() is None, "连续 3 次可疑失败仍没拉黑"


@respx.mock
def test_one_success_clears_the_suspect_count(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """中间成功一次就清零 —— 否则「连续」只是「累计」，健康代理迟早被啃掉。"""
    monkeypatch.setattr(setting, "PROXY_SUSPECT_BAN_AFTER", 3)
    pool = _pool()
    pool.report_suspect(f"http://{_P}")
    pool.report_suspect(f"http://{_P}")
    pool.report_good(f"http://{_P}")
    pool.report_suspect(f"http://{_P}")
    pool.report_suspect(f"http://{_P}")
    assert pool.get_proxy() == f"http://{_P}", "成功没有清零可疑计数"


@respx.mock
def test_success_also_resets_the_backoff_ladder(clock: list[float]) -> None:
    """退避是按**连续**失败算的，而池此前根本不知道「成功」这回事。

    没有这一条，一个跑了一万次成功、15 分钟内偶尔失败三次的代理
    会被退避到 4 倍冷却 —— 日志里那句「第 N 次连续失败」是假的。
    """
    pool = _pool()
    pool.report_bad(f"http://{_P}")  # 第 1 次 → 冷却 60s
    clock[0] += 61.0
    assert pool.get_proxy() == f"http://{_P}"
    pool.report_good(f"http://{_P}")
    pool.report_bad(f"http://{_P}")  # 成功过了，这该重新算第 1 次
    clock[0] += 61.0
    assert pool.get_proxy() == f"http://{_P}", (
        "成功之后再失败仍按第 2 次退避（120s）—— 连续失败计数没被清零"
    )


# ---- 接线：谁调谁 --------------------------------------------------------


class _RecordingPool:
    """只记调用，不做别的。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get_proxy(self) -> str | None:  # pragma: no cover - 接口凑数
        return None

    def report_bad(self, proxy: str) -> None:
        self.calls.append(("bad", proxy))

    def report_suspect(self, proxy: str) -> None:
        self.calls.append(("suspect", proxy))

    def report_good(self, proxy: str) -> None:
        self.calls.append(("good", proxy))


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> _RecordingPool:
    pool = _RecordingPool()
    monkeypatch.setattr("mineworker.network.downloader._common.get_proxy_pool", lambda: pool)
    return pool


def test_failure_is_routed_by_who_is_to_blame(recording: _RecordingPool) -> None:
    """分类对了、路由错了，一样是那个 bug。"""
    from mineworker.network.downloader._common import report_proxy_failure

    report_proxy_failure("http://p1", httpx.ConnectError("拨不通"))
    report_proxy_failure("http://p2", httpx.ReadTimeout("目标站太慢"))
    assert recording.calls == [("bad", "http://p1"), ("suspect", "http://p2")]


@respx.mock
def test_downloader_reports_success_so_the_count_is_really_consecutive(
    recording: _RecordingPool,
) -> None:
    """成功路径必须上报，否则「连续失败」永远只是「累计失败」。"""
    from mineworker.network.downloader._httpx import HttpxDownloader
    from mineworker.network.request import Request

    respx.get("https://t/x").mock(return_value=httpx.Response(200, text="ok"))
    dl = HttpxDownloader(timeout=5, verify=False, proxy="http://p1")
    try:
        assert dl.download(Request(url="https://t/x")).status_code == 200
    finally:
        dl.close()
    assert ("good", "http://p1") in recording.calls, "成功了却没告诉代理池"


class _LegacyPool:
    """升级前写的自定义代理池：只有 `get_proxy` / `report_bad`，纯鸭子类型。

    文档让自定义池继承 `ProxyPool`，但 `PROXY_POOL` 是 `load_object` 加载的，
    没有任何地方强制这件事 —— 本仓库自己的 `benchmarks/proxylab.StaticPool`
    就是这样。直接调新钩子会让这类池当场 `AttributeError`。
    """

    def __init__(self) -> None:
        self.bad: list[str] = []

    def get_proxy(self) -> str | None:  # pragma: no cover - 接口凑数
        return None

    def report_bad(self, proxy: str) -> None:
        self.bad.append(proxy)


def test_a_legacy_duck_typed_pool_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """老自定义池不能因为新增钩子就崩 —— 这条用例是被一次真的红逼出来的。"""
    from mineworker.network.downloader._common import (
        report_good_proxy,
        report_proxy_failure,
    )

    pool = _LegacyPool()
    monkeypatch.setattr("mineworker.network.downloader._common.get_proxy_pool", lambda: pool)
    report_good_proxy("http://p1")  # 没有 report_good：静默跳过
    report_proxy_failure("http://p1", httpx.ReadTimeout("慢"))  # 没有 report_suspect：不拉黑
    assert pool.bad == []
    report_proxy_failure("http://p1", httpx.ConnectError("拨不通"))  # 这条它有
    assert pool.bad == ["http://p1"]
