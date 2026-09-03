"""运行期统计：线程安全计数器 + 结束时的汇总输出。"""

from __future__ import annotations

import threading
import time
from collections import Counter

REQUEST_OK = "request_ok"
REQUEST_FAILED = "request_failed"
RETRY = "retry"
PARSE_ERROR = "parse_error"
DEDUP_DROPPED = "dedup_dropped"
DROPPED = "dropped"
ITEM = "item"
ITEM_DEDUP_DROPPED = "item_dedup_dropped"
ITEM_FAILED = "item_failed"


class Stats:
    def __init__(self) -> None:
        self._counter: Counter[str] = Counter()
        self._lock = threading.Lock()
        self.start_time = time.monotonic()

    def incr(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._counter[key] += n

    def get(self, key: str) -> int:
        with self._lock:
            return self._counter[key]

    def as_dict(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counter)

    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    def summary(self) -> str:
        d = self.as_dict()
        elapsed = self.elapsed()
        ok = d.get(REQUEST_OK, 0)
        rate = ok / elapsed if elapsed else 0.0
        return (
            f"用时 {elapsed:.1f}s | 请求成功 {ok} 失败 {d.get(REQUEST_FAILED, 0)} "
            f"| 重试 {d.get(RETRY, 0)} 丢弃 {d.get(DROPPED, 0)} "
            f"| 请求去重 {d.get(DEDUP_DROPPED, 0)} | 解析异常 {d.get(PARSE_ERROR, 0)} "
            f"| 入库 {d.get(ITEM, 0)} 条（去重 {d.get(ITEM_DEDUP_DROPPED, 0)}，"
            f"失败 {d.get(ITEM_FAILED, 0)}）| {rate:.1f} 请求/s"
        )
