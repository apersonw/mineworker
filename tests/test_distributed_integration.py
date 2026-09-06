"""分布式能力在**真 Redis + 真多进程**下的验证。

此前 839 行分布式代码全部只用 fakeredis + 单进程测过 —— 那既不含真正的并发竞争，
也不跨进程。而分布式恰恰是这个框架相对 AirSpider 的全部价值所在。

**判据由 HTTP 靶子给出，而不是问框架自己**：靶子记录每个路径被请求的次数，
这是框架外部的地面真相。「每个 URL 恰好一次」才是共享队列 + 共享去重的意义。

worker 函数与 Spider 类必须定义在**模块顶层**：macOS 的 multiprocessing 默认
spawn，子进程会重新 import 本模块，闭包和局部类没法 pickle。
"""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import time
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request as WRequest
from werkzeug.wrappers import Response as WResponse

pytestmark = pytest.mark.integration

PAGES = 24
_PROC_TIMEOUT = 90  # 挂起必须判失败，不能让 CI 卡死


@pytest.fixture
def clean_redis(redis_url: str) -> Iterator[str]:
    import redis as redis_lib

    client = redis_lib.from_url(redis_url, decode_responses=True)
    client.flushdb()
    yield redis_url
    client.flushdb()
    client.close()


def _serve(server: HTTPServer, hits: Any, page_delay: float = 0.0) -> None:
    """靶子：种子页给出 PAGES 个链接；每次请求都记一笔。

    ``page_delay`` 给续爬测试用 —— 本地靶子太快的话，第一个进程会在被杀之前
    就把全部页面抓完，续爬这条路径就根本没被触发（第一版就栽在这里）。
    """

    def seed(request: WRequest) -> WResponse:
        hits.append("/seed")
        links = "".join(f'<a href="/p/{i}">p{i}</a>' for i in range(PAGES))
        return WResponse(f"<html><body>{links}</body></html>", content_type="text/html")

    def page(request: WRequest) -> WResponse:
        hits.append(request.path)
        if page_delay:
            time.sleep(page_delay)
        return WResponse("<html><body><h1>ok</h1></body></html>", content_type="text/html")

    server.expect_request("/seed").respond_with_handler(seed)
    for i in range(PAGES):
        server.expect_request(f"/p/{i}").respond_with_handler(page)


def _serve_timed(server: HTTPServer, hits: Any) -> None:
    """同上，但连**到达时刻**一起记 —— 限速的判据是节奏，不是次数。"""

    def seed(request: WRequest) -> WResponse:
        hits.append(("/seed", time.time()))
        links = "".join(f'<a href="/p/{i}">p{i}</a>' for i in range(PAGES))
        return WResponse(f"<html><body>{links}</body></html>", content_type="text/html")

    def page(request: WRequest) -> WResponse:
        hits.append((request.path, time.time()))
        return WResponse("<html><body><h1>ok</h1></body></html>", content_type="text/html")

    server.expect_request("/seed").respond_with_handler(seed)
    for i in range(PAGES):
        server.expect_request(f"/p/{i}").respond_with_handler(page)


# ---- 顶层定义，供子进程 spawn 后重新 import ---------------------------
def _run_node(
    seed_url: str,
    redis_url: str,
    redis_key: str,
    keep: float = 0.0,
    delay: float = 0.0,
    global_throttle: bool = False,
) -> None:
    """一个「节点」：独立进程里跑一个 Spider。

    子进程不需要 monkeypatch —— get_redis() 读 setting.REDIS_URL，
    配上它就会连真 Redis，这正是生产里的真实路径。
    """
    import mineworker as mw
    from mineworker import setting
    from mineworker.utils import log

    setting.REDIS_URL = redis_url
    setting.ITEM_PIPELINES = []
    setting.LOG_LEVEL = "CRITICAL"
    setting.DONE_CHECK_INTERVAL = 0.2
    setting.DONE_CHECK_TIMES = 3
    setting.HEARTBEAT_INTERVAL = 0.3
    setting.HEARTBEAT_STALE = 5.0
    setting.SPIDER_THREAD_COUNT = 3
    setting.RANDOM_USER_AGENT = False
    setting.ROBOTS_OBEY = False
    setting.CONCURRENT_REQUESTS_PER_DOMAIN = 0
    setting.CIRCUIT_FAILURE_THRESHOLD = 0
    if keep:
        setting.SPIDER_MAX_RUNTIME = keep
    setting.DOWNLOAD_DELAY = delay
    # 抖动会让间隔在 ±50% 浮动，测出来的最小间隔就没法和配置值对账了
    setting.RANDOMIZE_DOWNLOAD_DELAY = False
    setting.GLOBAL_THROTTLE = global_throttle
    log.configure()

    class NodeSpider(mw.Spider):
        __redis_key__ = redis_key

        def start_requests(self):  # type: ignore[no-untyped-def]
            yield mw.Request(seed_url, callback=self.parse_seed)

        def parse_seed(self, request, response):  # type: ignore[no-untyped-def]
            for href in response.css("a::attr(href)").getall():
                yield mw.Request(response.urljoin(href), callback=self.parse_page)

        def parse_page(self, request, response):  # type: ignore[no-untyped-def]
            return None

    NodeSpider(redis_key=redis_key).start()


def _spawn(n: int, seed_url: str, redis_url: str, key: str, **node_kw: Any) -> list[mp.Process]:
    procs = [
        mp.Process(target=_run_node, args=(seed_url, redis_url, key), kwargs=node_kw, daemon=False)
        for _ in range(n)
    ]
    for p in procs:
        p.start()
    return procs


def _join_all(procs: list[mp.Process], timeout: float = _PROC_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    for p in procs:
        p.join(timeout=max(deadline - time.monotonic(), 1))
    alive = [p for p in procs if p.is_alive()]
    for p in alive:
        p.terminate()
        p.join(timeout=5)
    assert not alive, f"{len(alive)} 个节点没有自行退出 —— 结束检测在多进程下失效"


# ---- 核心：多节点不重复、不遗漏 ---------------------------------------
def test_three_nodes_no_duplicate_no_missing(httpserver: HTTPServer, clean_redis: str) -> None:
    """三个真进程共享一个 Redis 队列：每个 URL 恰好被抓一次。

    这是共享队列 + 共享去重的**全部意义**。fakeredis 单进程测不出来。
    """
    with mp.Manager() as mgr:
        hits = mgr.list()
        _serve(httpserver, hits)
        key = f"dist{os.getpid()}"
        procs = _spawn(3, httpserver.url_for("/seed"), clean_redis, key)
        _join_all(procs)
        counted = Counter(list(hits))

    pages = {p: c for p, c in counted.items() if p.startswith("/p/")}
    assert len(pages) == PAGES, f"漏抓：只见到 {len(pages)}/{PAGES} 个页面"
    dupes = {p: c for p, c in pages.items() if c != 1}
    assert not dupes, f"重复抓取：{dupes}"


def test_seed_lock_only_one_node_seeds(httpserver: HTTPServer, clean_redis: str) -> None:
    """acquire_once 的种子锁在真并发下必须只让一个进程注入种子。

    单进程里根本不存在这个竞争 —— 这是 SET NX EX 第一次被真正检验。
    """
    with mp.Manager() as mgr:
        hits = mgr.list()
        _serve(httpserver, hits)
        key = f"seed{os.getpid()}"
        procs = _spawn(3, httpserver.url_for("/seed"), clean_redis, key)
        _join_all(procs)
        counted = Counter(list(hits))

    assert counted["/seed"] == 1, f"种子页被抓了 {counted['/seed']} 次，种子锁没生效"


@pytest.mark.parametrize(
    ("sig", "label"),
    [(signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")],
)
def test_graceful_stop_returns_tasks_to_redis(
    httpserver: HTTPServer, clean_redis: str, sig: int, label: str
) -> None:
    """节点被信号停掉时，本地缓冲里的任务必须还回 Redis，不能永久丢失。

    **SIGTERM 曾经会丢**：框架只装了 SIGINT 处理器，而 collector 一次
    ``get_batch(COLLECTOR_TASK_COUNT-1)`` 会把上百个任务从 Redis 领走
    （``zpopmin`` 取走即删）搬进本地内存。SIGTERM 没有处理器 → 进程直接终止 →
    ``finally: _teardown()`` 不执行 → ``_on_shutdown`` 的「推回 Redis」逻辑没机会跑。

    实测那次：24 个任务里 SIGTERM 丢了 20 个，SIGINT 全数恢复。
    而 SIGTERM 正是 docker stop / K8s 驱逐 / systemctl stop 发的信号 ——
    MineWorkerHub 停任务用的也是它。
    """
    with mp.Manager() as mgr:
        hits = mgr.list()
        # 慢靶子：保证第一个进程来不及抓完，本地缓冲里确实压着任务
        _serve(httpserver, hits, page_delay=0.2)
        key = f"sig{label}{os.getpid()}"
        url = httpserver.url_for("/seed")

        first = mp.Process(target=_run_node, args=(url, clean_redis, key), daemon=False)
        first.start()
        time.sleep(1.0)
        os.kill(first.pid, sig)
        first.join(timeout=30)
        partial = len({h for h in hits if h.startswith("/p/")})

        second = mp.Process(target=_run_node, args=(url, clean_redis, key), daemon=False)
        second.start()
        _join_all([second])
        counted = Counter([h for h in list(hits) if h.startswith("/p/")])

    assert partial < PAGES, f"第一个进程就抓完了（{partial}），这轮没验到恢复"
    assert len(counted) == PAGES, (
        f"{label} 后永久丢失 {PAGES - len(counted)} 个任务 —— 本地缓冲没被推回 Redis"
    )
    dupes = {p: c for p, c in counted.items() if c != 1}
    assert not dupes, f"恢复过程造成重复：{dupes}"


def test_heartbeat_visible_across_processes(httpserver: HTTPServer, clean_redis: str) -> None:
    """多进程下心跳要真的写进共享 Redis，否则结束检测无从判断别的节点。"""
    import redis as redis_lib

    with mp.Manager() as mgr:
        hits = mgr.list()
        _serve(httpserver, hits)
        key = f"hb{os.getpid()}"
        procs = _spawn(2, httpserver.url_for("/seed"), clean_redis, key)

        client = redis_lib.from_url(clean_redis, decode_responses=True)
        seen_nodes = 0
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and seen_nodes < 2:
            seen_nodes = max(seen_nodes, client.hlen(f"mineworker:{key}:heartbeat") or 0)
            time.sleep(0.2)
        client.close()
        _join_all(procs)

    assert seen_nodes >= 2, f"只看到 {seen_nodes} 个节点的心跳，多进程心跳没生效"


# ---- 全局限速：N 个节点合起来才是配置的速率 ---------------------------
def test_global_throttle_lua_spaces_tickets(clean_redis: str, monkeypatch: Any) -> None:
    """先单独验 Lua 的取号算术，多进程测出问题时好分辨是算法还是并发。"""
    from mineworker import setting
    from mineworker.network import global_throttle

    # 用 monkeypatch 而不是直接赋值：跑测试时 pytest-randomly 会打乱顺序，
    # 改了不还原的全局 setting 会漏到别的用例里
    monkeypatch.setattr(setting, "REDIS_URL", clean_redis)
    global_throttle.reset()
    waits = [global_throttle.take_ticket("lua.example", 1.0) for _ in range(3)]

    assert all(w is not None for w in waits), "真 Redis 下不该走兜底分支"
    # 连号必须依次让开 1 秒：0、1、2
    assert waits[0] == pytest.approx(0.0, abs=0.05)
    assert waits[1] == pytest.approx(1.0, abs=0.05)
    assert waits[2] == pytest.approx(2.0, abs=0.05)


def _max_per_second(stamps: list[float]) -> int:
    """最繁忙的 1 秒窗口里落了几个请求 —— 也就是站点实际承受的峰值速率。"""
    ordered = sorted(stamps)
    return max(
        (sum(1 for t in ordered if s <= t < s + 1.0) for s in ordered),
        default=0,
    )


def _peak_rate(
    httpserver: HTTPServer,
    redis_url: str,
    hits: Any,
    tag: str,
    delay: float,
    global_on: bool,
) -> int:
    """跑一轮三节点，返回靶子看到的峰值到达速率。

    只看本轮**新增**的那些记录（`hits` 是两轮共用的）—— 不能靠重新注册 handler
    来隔离两轮：`expect_request` 是先注册先匹配，第二次注册根本不会被用到，
    于是第二轮读到的永远是空列表。
    """
    seen_before = len(hits)
    procs = _spawn(
        3,
        httpserver.url_for("/seed"),
        redis_url,
        f"thr{os.getpid()}{tag}",
        delay=delay,
        global_throttle=global_on,
    )
    _join_all(procs)
    fresh = list(hits)[seen_before:]
    stamps = [ts for path, ts in fresh if path.startswith("/p/")]
    assert len(stamps) == PAGES, f"漏抓：只见到 {len(stamps)}/{PAGES} 个页面"
    return _max_per_second(stamps)


def test_global_throttle_caps_rate_across_nodes(httpserver: HTTPServer, clean_redis: str) -> None:
    """三个真进程压同一个域：打开全局限速后，站点承受的速率才是配置的那个。

    **判据取自靶子记录的到达时刻**，不是问框架自己等了多久 —— 站点感受到的
    负载才是这件事的意义所在。

    开关两种情况**在同一次测试里各跑一遍**，而不是拆成两个参数化用例：
    对照组只用绝对阈值的话，在慢机器上（CI runner）可能仅仅因为跑不快就
    「没超速」，于是测试变成绿的却什么都没证明。同机比较则是自校准的 ——
    机器慢时两轮一起慢，而两者的**差距**仍然成立。
    """
    delay = 0.3
    allowed = round(1 / delay) + 1  # 3.33/s，放一格余量给网络抖动
    with mp.Manager() as mgr:
        hits = mgr.list()
        _serve_timed(httpserver, hits)
        peak_off = _peak_rate(httpserver, clean_redis, hits, "off", delay, global_on=False)
        peak_on = _peak_rate(httpserver, clean_redis, hits, "on", delay, global_on=True)

    assert peak_on <= allowed, f"打开全局限速后仍超速：峰值 {peak_on}/s > 允许的 {allowed}/s"
    assert peak_off > peak_on, (
        f"关掉全局限速后峰值没升高（{peak_off}/s vs {peak_on}/s）—— "
        "这轮对照不成立，说明测试根本没压到限速线"
    )
