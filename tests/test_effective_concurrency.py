"""`thread_count=` 覆盖了工作线程数，分片就必须跟着走。

每个 Spider 都接受 `thread_count=N` 覆盖 `setting.SPIDER_THREAD_COUNT`，而
`pool_limits` / `shard_count` / `loop_count` 三个函数原先直接读那个配置。
两者不联动时 `AirSpider(thread_count=64)` 配默认 `SPIDER_THREAD_COUNT=4`
会算出 **1 片 1 个循环** —— v4.34/v4.35 的分片修复静默不生效。

代价是量出来的（同样 64 个真实线程、50ms 靶子）：
同步 546 → 899 QPS，异步 198 → 775 QPS。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from mineworker import setting
from mineworker.core.spiders.air_spider import AirSpider
from mineworker.network.downloader import close_default_downloaders
from mineworker.network.downloader._async_httpx import loop_count
from mineworker.network.downloader._common import effective_concurrency, shard_count


class _Spider(AirSpider):
    def start_requests(self) -> Iterator[None]:  # pragma: no cover - 不真跑
        return iter(())


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    close_default_downloaders()  # 清掉上一条用例可能留下的并发值
    yield
    close_default_downloaders()


def test_thread_count_override_reaches_the_sharding(monkeypatch: pytest.MonkeyPatch) -> None:
    """`thread_count=64` + 默认配置 4 —— 分片要按 64 算，不是按 4。"""
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 4)
    monkeypatch.setattr(setting, "SESSION_SHARD_THREADS", 32)
    monkeypatch.setattr(setting, "ASYNC_THREADS_PER_LOOP", 16)
    _Spider(thread_count=64)
    assert effective_concurrency() == 64
    assert shard_count() == 2, f"64 线程只分了 {shard_count()} 片 —— 覆盖没传到下载器层"
    assert loop_count() == 4, f"64 线程只开了 {loop_count()} 个事件循环"


def test_without_override_it_follows_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """阴性对照：不覆盖时仍旧读配置。

    少了这一半，上一条在「永远返回 64」的实现下也会绿。
    """
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 4)
    monkeypatch.setattr(setting, "SESSION_SHARD_THREADS", 32)
    monkeypatch.setattr(setting, "ASYNC_THREADS_PER_LOOP", 16)
    _Spider()
    assert effective_concurrency() == 4
    assert shard_count() == 1
    assert loop_count() == 1


def test_two_spiders_in_one_process_take_the_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """全局下载器是它们共用的 —— 宁可多分片，也别按小的那个分。"""
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 4)
    _Spider(thread_count=64)
    _Spider(thread_count=8)
    assert effective_concurrency() == 64


def test_teardown_resets_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """不重置就会跨爬虫泄漏，下一个爬虫按上一个的线程数分片。"""
    monkeypatch.setattr(setting, "SPIDER_THREAD_COUNT", 4)
    _Spider(thread_count=64)
    assert effective_concurrency() == 64
    close_default_downloaders()
    assert effective_concurrency() == 4
