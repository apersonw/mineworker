"""per-domain 限速：单域并发上限 + 请求间隔。

框架此前唯一的节流是 `SPIDER_THREAD_COUNT`，抓多域名时所有线程可能全压在一个域上。
这里按域名分账：并发用信号量，间隔用「取号」排队。

并发上限是**进程内**的：分布式 N 个节点，单域在途请求就是 N 倍上限。
请求间隔则可以全局化 —— 打开 `GLOBAL_THROTTLE` 后由
[`global_throttle`](global_throttle.py) 用 Redis 记账，N 个节点合起来
才是配置的那个速率。
"""

from __future__ import annotations

import random
import threading
import time
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from mineworker import setting
from mineworker.network import global_throttle

if TYPE_CHECKING:
    from collections.abc import Iterator


def domain_of(url: str) -> str:
    """限速的分账键：小写 netloc（含端口）。

    不做子域归并 —— `www.a.com` 与 `a.com` 算两个域。简单可预测，
    猜用户意图只会带来惊喜。
    """
    return urlsplit(url).netloc.lower()


class DomainThrottle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_at: dict[str, float] = {}
        self._sems: dict[str, threading.Semaphore] = {}
        #: robots.txt 的 Crawl-delay 等来源写入的每域间隔覆盖
        self._domain_delay: dict[str, float] = {}

    # ------------------------------------------------------------------
    def _semaphore(self, domain: str) -> threading.Semaphore | None:
        limit = setting.CONCURRENT_REQUESTS_PER_DOMAIN
        if limit <= 0:
            return None
        with self._lock:
            sem = self._sems.get(domain)
            if sem is None:
                sem = threading.Semaphore(limit)
                self._sems[domain] = sem
            return sem

    def _take_ticket(self, domain: str) -> float:
        """排队取号，返回需要等待的秒数。**计算在锁内、睡眠在锁外。**

        持锁睡觉会让所有线程在同一把锁上串行；取号则让它们各自领到不同的
        起跑时刻，然后并行地各睡各的。
        """
        with self._lock:
            override = self._domain_delay.get(domain, 0.0)
        # 站点自己声明的节奏（robots.txt 的 Crawl-delay）不该低于全局默认值
        delay = max(setting.DOWNLOAD_DELAY, override)
        if delay > 0 and setting.RANDOMIZE_DOWNLOAD_DELAY:
            # 整齐的请求节奏本身就是机器人特征
            delay *= random.uniform(0.5, 1.5)
        if setting.GLOBAL_THROTTLE:
            # 不能因为 delay<=0 就跳过这次 Redis 往返：penalize() 写的整域冷却
            # 也存在同一个 key 里，而 DOWNLOAD_DELAY 默认就是 0 —— 跳过会让 429
            # 冷却对默认配置静默失效（本地版在这里栽过一次，见下面的注释）
            wait = global_throttle.take_ticket(domain, delay)
            if wait is not None:
                return wait
            # None = Redis 不可用，往下走本地限速兜底
        with self._lock:
            now = time.monotonic()
            # 用 0.0 作缺省再取 max(now, ...) 是安全的：外层的 max 保证了
            # 「没记录过」等价于「现在就能跑」，与机器 uptime 无关。
            # （本项目在裸用 0.0 当 monotonic 哨兵上栽过，别照着简化掉这个 max）
            ready = max(now, self._next_at.get(domain, 0.0))
            # 注意不能在 delay<=0 时提前返回：penalize() 写的整域冷却也存在
            # _next_at 里，而 DOWNLOAD_DELAY 默认就是 0 —— 提前返回会让 429
            # 冷却对默认配置的所有人静默失效
            if delay > 0 or ready > now:
                self._next_at[domain] = ready + max(delay, 0.0)
        return ready - now

    @contextmanager
    def slot(self, url: str) -> Iterator[None]:
        """占一个该域的并发名额并完成间隔等待；退出时必定释放。"""
        domain = domain_of(url)
        sem = self._semaphore(domain)
        if sem is not None:
            sem.acquire()
        try:
            # 等待期间占着名额是刻意的：DOWNLOAD_DELAY=1 时本来就不该有 8 个并发在飞
            wait = self._take_ticket(domain)
            if wait > 0:
                time.sleep(wait)
            yield
        finally:
            if sem is not None:
                sem.release()

    # ------------------------------------------------------------------
    def penalize(self, url: str, seconds: float) -> None:
        """把整个域的下次可请求时间推后（429 / 503 用）。

        只让撞上 429 的那个 worker 等是没用的 —— 其余 worker 会继续满速打同一个域，
        退避形同虚设。这里把冷却抑制到域级别，所有 worker 一起避开。
        """
        if seconds <= 0:
            return
        domain = domain_of(url)
        if setting.GLOBAL_THROTTLE:
            # 全局记一笔，让**所有节点**一起避开 —— 只有本节点退避是没用的，
            # 别的节点会继续满速打同一个域。返回值（要等多久）在这里不关心
            global_throttle.take_ticket(domain, seconds)
        # 本地也记一笔：Redis 中途失联退回本地限速时，惩罚不会跟着丢
        with self._lock:
            now = time.monotonic()
            ready = max(now, self._next_at.get(domain, 0.0))
            self._next_at[domain] = ready + seconds

    def set_domain_delay(self, domain: str, seconds: float) -> None:
        """为单个域设置间隔下限（robots.txt 的 ``Crawl-delay`` 用）。"""
        with self._lock:
            self._domain_delay[domain] = max(seconds, 0.0)

    def reset(self) -> None:
        """清空所有域的状态（测试用；也可在爬虫重启时调用）。"""
        with self._lock:
            self._next_at.clear()
            self._sems.clear()
            self._domain_delay.clear()


#: 进程级单例
_default = DomainThrottle()


def slot(url: str) -> AbstractContextManager[None]:
    return _default.slot(url)


def penalize(url: str, seconds: float) -> None:
    _default.penalize(url, seconds)


def set_domain_delay(domain: str, seconds: float) -> None:
    _default.set_domain_delay(domain, seconds)


def reset() -> None:
    _default.reset()
