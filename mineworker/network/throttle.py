"""per-domain 限速：单域并发上限 + 请求间隔。

框架此前唯一的节流是 `SPIDER_THREAD_COUNT`，抓多域名时所有线程可能全压在一个域上。
这里按域名分账：并发用信号量，间隔用「取号」排队。

.. warning::
   **这是进程内限速。** 分布式 `Spider` 起 N 个节点，目标站承受的就是 N 倍。
   真正的全局限速需要 Redis 令牌桶，目前没做 —— 别以为配了这个就一定礼貌。
"""

from __future__ import annotations

import random
import threading
import time
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from mineworker import setting

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
        delay = setting.DOWNLOAD_DELAY
        if delay > 0 and setting.RANDOMIZE_DOWNLOAD_DELAY:
            # 整齐的请求节奏本身就是机器人特征
            delay *= random.uniform(0.5, 1.5)
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
        with self._lock:
            now = time.monotonic()
            ready = max(now, self._next_at.get(domain, 0.0))
            self._next_at[domain] = ready + seconds

    def reset(self) -> None:
        """清空所有域的状态（测试用；也可在爬虫重启时调用）。"""
        with self._lock:
            self._next_at.clear()
            self._sems.clear()


#: 进程级单例
_default = DomainThrottle()


def slot(url: str) -> AbstractContextManager[None]:
    return _default.slot(url)


def penalize(url: str, seconds: float) -> None:
    _default.penalize(url, seconds)


def reset() -> None:
    _default.reset()
