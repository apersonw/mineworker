"""robots.txt 支持。

用标准库 `urllib.robotparser`，不引第三方依赖。按域缓存规则，并把 robots.txt 里的
`Crawl-delay` 喂给 per-domain 限速。

抓取失败时**放行**（fail-open）：`/robots.txt` 一次瞬时 500 不该让整个爬虫停摆，
而且「什么都不抓也不报错」是极难排查的故障形态。
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from mineworker import setting
from mineworker.network import throttle
from mineworker.utils.log import get_logger

log = get_logger("robots")


class _Entry:
    __slots__ = ("fetched_at", "parser", "text")

    def __init__(self, parser: RobotFileParser | None, text: str, fetched_at: float) -> None:
        #: None 表示「抓不到 / 没有 robots.txt」→ 放行全部
        self.parser = parser
        #: 原文；用来解析标准库会丢掉的小数 Crawl-delay
        self.text = text
        self.fetched_at = fetched_at


def parse_crawl_delay(text: str, useragent: str) -> float | None:
    """从 robots.txt 原文解析 ``Crawl-delay``。

    不能直接用 `RobotFileParser.crawl_delay()`：标准库只接受**整数**
    （它用 `line[1].strip().isdigit()` 判断，`"0.5".isdigit()` 是 False），
    小数值会被静默丢弃 —— 那等于爬得比站点要求的还快，方向错了。
    """
    delays: dict[str, float] = {}
    group: list[str] = []
    seen_rule = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            # 规则行之后再出现 user-agent，说明这是新的一组
            if seen_rule:
                group = []
                seen_rule = False
            group.append(value.lower())
        elif key == "crawl-delay":
            seen_rule = True
            try:
                seconds = float(value)
            except ValueError:
                continue
            for agent in group:
                delays[agent] = seconds
        elif key in ("allow", "disallow", "sitemap"):
            seen_rule = True

    ua = useragent.lower()
    # 精确匹配优先于通配组
    return delays.get(ua, delays.get("*"))


def robots_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


class RobotsCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._domain_locks: dict[str, threading.Lock] = {}
        self._warned_can_fetch = False

    # ------------------------------------------------------------------
    def _domain_lock(self, domain: str) -> threading.Lock:
        with self._lock:
            lock = self._domain_locks.get(domain)
            if lock is None:
                lock = threading.Lock()
                self._domain_locks[domain] = lock
            return lock

    def _fresh(self, entry: _Entry) -> bool:
        ttl = setting.ROBOTS_CACHE_TTL
        if ttl <= 0:
            return True
        return (time.monotonic() - entry.fetched_at) < ttl

    def _entry(self, url: str) -> _Entry:
        domain = throttle.domain_of(url)
        with self._lock:
            entry = self._entries.get(domain)
        if entry is not None and self._fresh(entry):
            return entry

        # 每域一把锁：多个 worker 同时首访同一个域时只抓一次，其余等结果
        with self._domain_lock(domain):
            with self._lock:
                entry = self._entries.get(domain)
            if entry is not None and self._fresh(entry):
                return entry
            parser, text = self._fetch(url)
            entry = _Entry(parser, text, time.monotonic())
            with self._lock:
                self._entries[domain] = entry
            self._apply_crawl_delay(domain, entry)
            return entry

    def _fetch(self, url: str) -> tuple[RobotFileParser | None, str]:
        """抓并解析 robots.txt；任何抓不到的情形都返回 (None, "")（= 放行）。"""
        target = robots_url(url)
        # 直接用下载器而不是走 parser_control：否则会递归触发 robots 检查
        from mineworker.network.downloader._httpx import HttpxDownloader
        from mineworker.network.request import Request

        try:
            with HttpxDownloader() as dl:
                resp = dl.download(Request(target, random_user_agent=False))
        except Exception as exc:
            log.warning("抓 {} 失败，按「无限制」处理：{!r}", target, exc)
            return None, ""

        if resp.status_code == 404 or 400 <= resp.status_code < 500:
            log.debug("{} 返回 {}，按「无限制」处理", target, resp.status_code)
            return None, ""
        if not (200 <= resp.status_code < 300):
            # 5xx：fail-open，但要让人看见
            log.warning("{} 返回 {}，按「无限制」处理", target, resp.status_code)
            return None, ""

        text = resp.text
        parser = RobotFileParser()
        try:
            parser.parse(text.splitlines())
        except Exception as exc:  # 畸形 robots.txt
            log.warning("解析 {} 失败，按「无限制」处理：{!r}", target, exc)
            return None, ""
        return parser, text

    def _apply_crawl_delay(self, domain: str, entry: _Entry) -> None:
        """把 robots.txt 声明的 Crawl-delay 交给限速器。"""
        if entry.parser is None:
            return
        seconds = parse_crawl_delay(entry.text, setting.ROBOTS_USER_AGENT)
        if seconds is None:
            return
        if seconds > 0:
            log.info("{} 的 robots.txt 声明 Crawl-delay={}s，已应用", domain, seconds)
            throttle.set_domain_delay(domain, seconds)

    # ------------------------------------------------------------------
    def allowed(self, url: str) -> bool:
        parser = self._entry(url).parser
        if parser is None:
            return True
        try:
            return bool(parser.can_fetch(setting.ROBOTS_USER_AGENT, url))
        except Exception:
            # 静默放行是危险的：若对每个 URL 都抛异常，会「全部放行」而毫无提示。
            # 只警告一次，避免刷屏，但至少让人知道 robots 判定实际没在工作。
            if not self._warned_can_fetch:
                self._warned_can_fetch = True
                log.warning("robots 规则判定异常，按「无限制」处理", exc_info=True)
            return True

    def crawl_delay(self, url: str) -> float | None:
        entry = self._entry(url)
        if entry.parser is None:
            return None
        return parse_crawl_delay(entry.text, setting.ROBOTS_USER_AGENT)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._domain_locks.clear()


#: 进程级单例
_default = RobotsCache()


def allowed(url: str) -> bool:
    """`ROBOTS_OBEY` 关闭时恒为 True，且不会产生任何请求。"""
    if not setting.ROBOTS_OBEY:
        return True
    return _default.allowed(url)


def crawl_delay(url: str) -> float | None:
    return _default.crawl_delay(url)


def reset() -> None:
    _default.reset()
