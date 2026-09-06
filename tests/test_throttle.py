"""per-domain 限速：并发上限、请求间隔、整域冷却。"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Iterator

import pytest

from mineworker import setting
from mineworker.network import throttle
from mineworker.network.throttle import DomainThrottle, domain_of


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    throttle.reset()
    yield
    throttle.reset()


# ---- 域名分账 --------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://A.com/x", "a.com"),
        ("http://a.com:8080/y", "a.com:8080"),
        ("https://www.a.com/", "www.a.com"),
    ],
)
def test_domain_key(url: str, expected: str) -> None:
    assert domain_of(url) == expected


def test_subdomains_are_separate_domains() -> None:
    """不猜用户意图：www.a.com 与 a.com 各算各的。"""
    assert domain_of("https://www.a.com/") != domain_of("https://a.com/")


# ---- 并发上限 --------------------------------------------------------
def test_concurrency_cap_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "CONCURRENT_REQUESTS_PER_DOMAIN", 2)
    monkeypatch.setattr(setting, "DOWNLOAD_DELAY", 0.0)
    th = DomainThrottle()
    peak = 0
    live = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal peak, live
        with th.slot("https://a.com/"):
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.05)
            with lock:
                live -= 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 2, f"单域并发应被压到 2，实际 {peak}"


def test_zero_means_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "CONCURRENT_REQUESTS_PER_DOMAIN", 0)
    monkeypatch.setattr(setting, "DOWNLOAD_DELAY", 0.0)
    th = DomainThrottle()
    peak = 0
    live = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal peak, live
        with th.slot("https://a.com/"):
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.05)
            with lock:
                live -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 6


def test_different_domains_do_not_share_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 域占满不该拖住 B 域。"""
    monkeypatch.setattr(setting, "CONCURRENT_REQUESTS_PER_DOMAIN", 1)
    monkeypatch.setattr(setting, "DOWNLOAD_DELAY", 0.0)
    th = DomainThrottle()
    started = threading.Event()

    def hog() -> None:
        with th.slot("https://a.com/"):
            started.set()
            time.sleep(0.3)

    t = threading.Thread(target=hog)
    t.start()
    started.wait(timeout=1)
    t0 = time.monotonic()
    with th.slot("https://b.com/"):  # 不该被 A 域堵住
        pass
    assert time.monotonic() - t0 < 0.15
    t.join()


def test_slot_released_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """名额必须在异常时也释放。

    这正是限速没做成中间件的原因：parser_control 里下载抛异常时
    process_response 不会执行，中间件拿的名额会泄漏。
    """
    monkeypatch.setattr(setting, "CONCURRENT_REQUESTS_PER_DOMAIN", 1)
    monkeypatch.setattr(setting, "DOWNLOAD_DELAY", 0.0)
    th = DomainThrottle()
    with pytest.raises(RuntimeError), th.slot("https://a.com/"):
        raise RuntimeError("下载炸了")
    # 名额若泄漏，这里会永久阻塞
    done = threading.Event()

    def again() -> None:
        with th.slot("https://a.com/"):
            done.set()

    t = threading.Thread(target=again, daemon=True)
    t.start()
    assert done.wait(timeout=2), "异常后名额泄漏了"


# ---- 请求间隔 --------------------------------------------------------
def test_delay_spaces_out_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "CONCURRENT_REQUESTS_PER_DOMAIN", 0)
    monkeypatch.setattr(setting, "DOWNLOAD_DELAY", 0.1)
    monkeypatch.setattr(setting, "RANDOMIZE_DOWNLOAD_DELAY", False)
    th = DomainThrottle()
    stamps: list[float] = []
    for _ in range(4):
        with th.slot("https://a.com/"):
            stamps.append(time.monotonic())
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    assert all(g >= 0.08 for g in gaps), f"间隔应 ≥0.1s，实际 {gaps}"


def test_delay_is_per_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "CONCURRENT_REQUESTS_PER_DOMAIN", 0)
    monkeypatch.setattr(setting, "DOWNLOAD_DELAY", 0.3)
    monkeypatch.setattr(setting, "RANDOMIZE_DOWNLOAD_DELAY", False)
    th = DomainThrottle()
    with th.slot("https://a.com/"):
        pass
    t0 = time.monotonic()
    with th.slot("https://b.com/"):  # 另一个域，不该等
        pass
    assert time.monotonic() - t0 < 0.1


def test_randomize_stays_in_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """抖动是 ±50%，不能跑出区间。"""
    monkeypatch.setattr(setting, "CONCURRENT_REQUESTS_PER_DOMAIN", 0)
    monkeypatch.setattr(setting, "DOWNLOAD_DELAY", 1.0)
    monkeypatch.setattr(setting, "RANDOMIZE_DOWNLOAD_DELAY", True)
    th = DomainThrottle()
    # 取号是累积排队（第 N 个号排在第 N-1 个之后），所以要看每次「往后推了多少」，
    # 而不是返回的等待时长本身 —— 后者会随排队递增
    deltas: list[float] = []
    prev: float | None = None
    for _ in range(20):
        th._take_ticket("a.com")
        cur = th._next_at["a.com"]
        if prev is not None:
            deltas.append(cur - prev)
        prev = cur
    assert all(0.4 <= d <= 1.6 for d in deltas), f"抖动越界：{deltas}"
    assert len({round(d, 3) for d in deltas}) > 1, "应该真的有抖动"


def test_no_delay_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """DOWNLOAD_DELAY 默认 0 —— 不给所有人降速。"""
    monkeypatch.setattr(setting, "DOWNLOAD_DELAY", 0.0)
    th = DomainThrottle()
    t0 = time.monotonic()
    for _ in range(5):
        with th.slot("https://a.com/"):
            pass
    assert time.monotonic() - t0 < 0.05


# ---- 整域冷却（429 / 503）--------------------------------------------
def test_penalize_delays_whole_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 的冷却要作用于整个域，不只是撞上的那个请求。"""
    monkeypatch.setattr(setting, "CONCURRENT_REQUESTS_PER_DOMAIN", 0)
    monkeypatch.setattr(setting, "DOWNLOAD_DELAY", 0.0)
    th = DomainThrottle()
    th.penalize("https://a.com/x", 0.3)
    t0 = time.monotonic()
    with th.slot("https://a.com/other"):  # 同域的别的 URL 也要等
        pass
    assert time.monotonic() - t0 >= 0.25


def test_penalize_does_not_affect_other_domains() -> None:
    th = DomainThrottle()
    th.penalize("https://a.com/", 1.0)
    t0 = time.monotonic()
    with th.slot("https://b.com/"):
        pass
    assert time.monotonic() - t0 < 0.1


@pytest.mark.parametrize("seconds", [0.0, -5.0])
def test_penalize_ignores_non_positive(seconds: float) -> None:
    th = DomainThrottle()
    th.penalize("https://a.com/", seconds)
    t0 = time.monotonic()
    with th.slot("https://a.com/"):
        pass
    assert time.monotonic() - t0 < 0.1


def test_penalty_must_be_capped_by_caller() -> None:
    """惩罚时长由调用方封顶 —— throttle 本身不认识 RETRY_AFTER_MAX。

    这条是踩坑记录：`Retry-After: 3600` 曾让整个域冷却一小时，导致所有 worker
    睡死在 throttle 里、爬虫在该域上彻底停摆。现在 parser_control 用
    `min(cooldown, RETRY_AFTER_MAX)` 封顶 —— 既然已经决定放弃这个请求，
    就不该再给整个域挂一小时的冷却。
    """
    th = DomainThrottle()
    th.penalize("https://a.com/", 3600.0)
    # throttle 会老实照做，所以封顶必须发生在调用方
    with th._lock:
        scheduled = th._next_at["a.com"] - time.monotonic()
    assert scheduled > 3000, "throttle 不做封顶，封顶是调用方的责任"


# ---- 全局限速（GLOBAL_THROTTLE）---------------------------------------
def test_global_throttle_falls_back_to_local_when_redis_down(monkeypatch) -> None:
    """Redis 连不上时退回**进程内限速**，而不是变成不限速。

    限速器一挂就放开手脚打目标站，是这里最不该有的失败模式 —— 所以断言的是
    「仍然在等」，不是「没报错」。
    """
    from mineworker.network import global_throttle

    global_throttle.reset()
    monkeypatch.setattr(setting, "GLOBAL_THROTTLE", True)
    monkeypatch.setattr(setting, "DOWNLOAD_DELAY", 0.05)
    monkeypatch.setattr(setting, "RANDOMIZE_DOWNLOAD_DELAY", False)
    monkeypatch.setattr(
        "mineworker.db.redisdb.get_redis",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Redis 连不上")),
    )

    th = throttle.DomainThrottle()
    assert th._take_ticket("example.com") == pytest.approx(0.0, abs=0.01)
    # 第二次必须还在排队 —— 说明本地兜底真的接上了
    assert th._take_ticket("example.com") >= 0.04


def test_global_throttle_penalize_also_recorded_locally(monkeypatch) -> None:
    """全局惩罚同时落一份本地：Redis 中途失联时冷却不会跟着丢。"""
    from mineworker.network import global_throttle

    global_throttle.reset()
    monkeypatch.setattr(setting, "GLOBAL_THROTTLE", True)
    monkeypatch.setattr(
        "mineworker.db.redisdb.get_redis",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Redis 连不上")),
    )

    th = throttle.DomainThrottle()
    th.penalize("https://example.com/x", 5.0)
    assert th._take_ticket("example.com") >= 4.5
