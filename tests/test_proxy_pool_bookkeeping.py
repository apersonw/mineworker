"""代理池按代理做键的四本账，得有人回收。

两个 bug 同源 —— 记账只增不减：

1. **用满次数的代理再也回不来。** `PROXY_MAX_USE_TIMES` 到了就 `_drop`，
   而 `_fetch` 又会把它补回池里，可 `_use_count` 从不清零 —— 补回来只能再用
   一次就又被丢。实测 3 个代理的池，第一轮用完后稳态退化成**每秒只取到 3 个**，
   还每秒去拉一次供应商的列表接口，永远不停。文档写的是「用满这么多次后
   **轮换**」，不是终身配额。默认 `PROXY_MAX_USE_TIMES=100`，所以这事要跑到
   第 100×N 个请求之后才出现 —— 短测试永远看不见。
2. **轮换型代理源会把内存吃掉。** 很多代理商的提取接口每次返回全新 IP，
   实测 3000 次 `get_proxy` 之后 `_use_count` 里躺着 1500 条，只增不减。
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
    now = [1000.0]
    monkeypatch.setattr("mineworker.network.proxy_pool.api.time.monotonic", lambda: now[0])
    return now


@pytest.fixture(autouse=True)
def _fast_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "PROXY_MIN_INTERVAL", 0.0)


@respx.mock
def test_a_used_up_proxy_is_usable_again_after_it_comes_back(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """用满 → 被丢 → 补回池里 → **应该能重新用满一轮**，而不是只能再用一次。

    判据是**拉了几次列表**，不是「取到没取到」：计数不清零时，补回来的代理
    在下一次使用就又越过上限被丢出池，于是**每个请求都要重新拉一次列表**。
    第一版用例断言的是「取到了代理」，而带着 bug 也能取到（每次都重拉一遍）——
    变异验证把这条假绿抓了出来。
    """
    monkeypatch.setattr(setting, "PROXY_MAX_USE_TIMES", 3)
    route = respx.get(_LIST).mock(return_value=httpx.Response(200, text="1.1.1.1:80"))
    pool = ApiProxyPool(_LIST)

    for _ in range(9):
        assert pool.get_proxy() == "http://1.1.1.1:80"
    # 9 次使用 = 3 轮，每轮把池排空一次 → 正好 3 次拉取
    assert route.call_count == 3, (
        f"9 次使用拉了 {route.call_count} 次列表（应为 3）—— "
        "补回池里的代理只能再用一次就又被丢，每个请求都在重新拉取"
    )


@respx.mock
def test_bookkeeping_stays_bounded_with_rotating_proxies(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """每次拉取都是新 IP 时，记账不能跟着一起涨。"""
    monkeypatch.setattr(setting, "PROXY_MAX_USE_TIMES", 2)
    batch = [0]

    def _next(_request: httpx.Request) -> httpx.Response:
        batch[0] += 1
        return httpx.Response(200, text="\n".join(f"10.0.{batch[0]}.{i}:80" for i in range(5)))

    respx.get(_LIST).mock(side_effect=_next)
    pool = ApiProxyPool(_LIST)
    for _ in range(300):
        pool.get_proxy()
    assert batch[0] > 5, "根本没轮换，这条用例没测到东西"
    assert len(pool._use_count) <= 10, (
        f"取了 300 次之后 _use_count 里有 {len(pool._use_count)} 条 —— 记账在漏"
    )


@respx.mock
def test_expired_bans_are_reclaimed(clock: list[float]) -> None:
    """冷却过期的记录靠 `_is_banned` 顺手删 —— 而「顺手」对一个再也不会出现的
    代理永远不来。"""
    respx.get(_LIST).mock(return_value=httpx.Response(200, text="1.1.1.1:80"))
    pool = ApiProxyPool(_LIST)
    pool.report_bad("http://1.1.1.1:80")
    assert pool._banned
    clock[0] += setting.PROXY_BAN_SECONDS + 1
    pool.get_proxy()  # 触发一次 _fetch → _prune
    assert not pool._banned, "过期的冷却记录没被回收"


@respx.mock
def test_pruning_does_not_weaken_the_backoff(clock: list[float]) -> None:
    """**防回收做过头**：退避阶梯不能被顺手清掉。

    `_fails` 记的是连续失败次数，冷却一过它就既不在池里也不在冷却里 ——
    要是按「不在流通中」一起清掉，一个时好时坏的代理永远escalate 不上去，
    只会在 60 秒那一档来回跳。
    """
    respx.get(_LIST).mock(return_value=httpx.Response(200, text="1.1.1.1:80"))
    pool = ApiProxyPool(_LIST)
    pool.report_bad("http://1.1.1.1:80")  # 第 1 次 → 60s
    clock[0] += setting.PROXY_BAN_SECONDS + 1
    assert pool.get_proxy() == "http://1.1.1.1:80"  # 顺带触发 _prune
    pool.report_bad("http://1.1.1.1:80")  # 第 2 次 → 该是 120s，不是 60s
    clock[0] += setting.PROXY_BAN_SECONDS + 1  # 只过了 61 秒
    assert pool.get_proxy() is None, "退避没有翻倍 —— _prune 把连续失败计数清掉了"
