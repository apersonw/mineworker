"""代理失败是**冷却**，不是永久放逐。

原来 `report_bad` 把代理加进一个只进不出的 set，而 `_fetch` 又拒绝放回 set 里的 ——
**一次瞬时错误就永久踢掉一个代理**。而瞬时错误是常态：压测时一个健康的本地
tinyproxy 在 960 个请求里也重置了 3 条 keep-alive 连接。

后果不是崩溃，是静默降级：池被慢慢啃空，配合 `PROXY_ALLOW_DIRECT=False`（默认），
后面每个请求都先等满 `PROXY_WAIT_TIMEOUT`（默认 30s）再失败。跑 A/B 压测时
600 个请求里一半以上卡在这个 30 秒上，整轮数据作废 —— 这个 bug 就是那时撞出来的。

这里用注入的时钟而不是真 sleep：要断言的是「冷却期一过就回来」，
拿真时间等 60 秒的测试没人会留着。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mineworker import setting
from mineworker.network.proxy_pool.api import ApiProxyPool

_LIST = "https://p/list"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """可控时钟。返回一个单元素列表，改它就是改「现在几点」。"""
    now = [1000.0]
    monkeypatch.setattr("mineworker.network.proxy_pool.api.time.monotonic", lambda: now[0])
    return now


def _pool(text: str = "1.1.1.1:80") -> ApiProxyPool:
    respx.get(_LIST).mock(return_value=httpx.Response(200, text=text))
    return ApiProxyPool(_LIST)


@respx.mock
def test_single_failure_does_not_kill_the_pool_forever(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """核心用例：唯一的代理失败一次，冷却期后必须自己回来。

    修复前这里会永远拿到 None —— 池空 → `_fetch` 重拉 → 因为在黑名单里而拒绝放回。
    """
    monkeypatch.setattr(setting, "PROXY_BAN_SECONDS", 60.0)
    monkeypatch.setattr(setting, "PROXY_MIN_INTERVAL", 0.0)
    pool = _pool()

    assert pool.get_proxy() == "http://1.1.1.1:80"
    pool.report_bad("http://1.1.1.1:80")

    # 冷却期内：确实不该再发出去
    assert pool.get_proxy() is None

    # 冷却期一过：必须自己回来
    clock[0] += 61.0
    assert pool.get_proxy() == "http://1.1.1.1:80", "冷却结束后代理没有回到池里"


@respx.mock
def test_repeated_failures_back_off_exponentially(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续失败要越冷却越久 —— 否则一个真坏掉的代理会被无限重试。"""
    monkeypatch.setattr(setting, "PROXY_BAN_SECONDS", 10.0)
    monkeypatch.setattr(setting, "PROXY_BAN_MAX_SECONDS", 1000.0)
    monkeypatch.setattr(setting, "PROXY_MIN_INTERVAL", 0.0)
    pool = _pool()

    for expected in (10.0, 20.0, 40.0):
        assert pool.get_proxy() == "http://1.1.1.1:80"
        pool.report_bad("http://1.1.1.1:80")
        # 差一点点还不该放出来
        clock[0] += expected - 1
        assert pool.get_proxy() is None, f"冷却 {expected}s，第 {expected - 1}s 就放出来了"
        clock[0] += 2
        assert pool.get_proxy() == "http://1.1.1.1:80"
        # 上面这次 get 把它取出来了，下一轮继续报错累加


@respx.mock
def test_backoff_resets_after_a_long_healthy_stretch(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """中间好好跑了很久，退避要重新从头算。

    没有这一条的话，一个跑几天的进程会把偶发失败累积成永久放逐 ——
    正是这个 bug 的另一种形态。
    """
    monkeypatch.setattr(setting, "PROXY_BAN_SECONDS", 10.0)
    monkeypatch.setattr(setting, "PROXY_BAN_MAX_SECONDS", 100.0)
    monkeypatch.setattr(setting, "PROXY_MIN_INTERVAL", 0.0)
    pool = _pool()

    pool.report_bad("http://1.1.1.1:80")  # 第 1 次 → 冷却 10s
    clock[0] += 20.0
    pool.report_bad("http://1.1.1.1:80")  # 第 2 次 → 冷却 20s
    clock[0] += 200.0  # 远超 BAN_MAX，视为已恢复
    pool.report_bad("http://1.1.1.1:80")  # 应当按「第 1 次」算 → 10s

    clock[0] += 11.0
    assert pool.get_proxy() == "http://1.1.1.1:80", "退避没有重新计数"


@respx.mock
def test_healthy_proxies_survive_a_neighbours_failure(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个代理失败不能牵连别的 —— 冷却是按代理记的。"""
    monkeypatch.setattr(setting, "PROXY_MIN_INTERVAL", 0.0)
    pool = _pool("1.1.1.1:80\n2.2.2.2:80")
    pool.get_proxy()
    pool.report_bad("http://1.1.1.1:80")

    got = {pool.get_proxy() for _ in range(5)}
    assert got == {"http://2.2.2.2:80"}


@respx.mock
def test_zero_means_forever_for_anyone_who_wants_the_old_behaviour(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PROXY_BAN_SECONDS=0` 保留旧的永久拉黑 —— 有人可能确实要这个。"""
    monkeypatch.setattr(setting, "PROXY_BAN_SECONDS", 0.0)
    monkeypatch.setattr(setting, "PROXY_MIN_INTERVAL", 0.0)
    pool = _pool()

    pool.report_bad("http://1.1.1.1:80")
    clock[0] += 10**9
    assert pool.get_proxy() is None
