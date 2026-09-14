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
ROBOTS_DROPPED = "robots_dropped"
CONTENT_TYPE_DROPPED = "content_type_dropped"
ITEM = "item"
ITEM_DEDUP_DROPPED = "item_dedup_dropped"
ITEM_FAILED = "item_failed"
DEDUP_DEGRADED = "dedup_degraded"  # 去重不可用、按「没见过」放行的批次数

#: 机器可读摘要的行首标记。爬虫结束时打一行 `<marker> <一行 JSON>`，
#: 供 NetspyHub 之类的编排层读容器日志用 —— 见 `Stats.as_summary_dict`。
#: **这是对外契约**：改前缀 / 改字段名都是破坏性变更（Hub 硬编码解析它）。
RUN_SUMMARY_MARKER = "NETSPY_RUN_SUMMARY"
#: 摘要的结构版本；字段有增删时 +1，消费方据此兼容
RUN_SUMMARY_SCHEMA = 1


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

    def as_summary_dict(self) -> dict[str, int | float]:
        """结构化摘要：字段名稳定、值都是数字，供机器解析。

        和给人看的 `summary()` 同源（同一份计数器），但这份是**契约** ——
        NetspyHub 读容器日志、按 `RUN_SUMMARY_MARKER` 找到这行 JSON，
        据此区分「跑了」（exit 0）和「抓了」（request_ok / items > 0）。
        运行上下文（run_id / namespace / spider）由调度器合并进来，不在这里。
        """
        d = self.as_dict()
        elapsed = self.elapsed()
        ok = d.get(REQUEST_OK, 0)
        return {
            "schema": RUN_SUMMARY_SCHEMA,
            "elapsed": round(elapsed, 3),
            "request_ok": ok,
            "request_failed": d.get(REQUEST_FAILED, 0),
            "retry": d.get(RETRY, 0),
            "dropped": d.get(DROPPED, 0),
            "robots_dropped": d.get(ROBOTS_DROPPED, 0),
            "content_type_dropped": d.get(CONTENT_TYPE_DROPPED, 0),
            "dedup_dropped": d.get(DEDUP_DROPPED, 0),
            "parse_error": d.get(PARSE_ERROR, 0),
            "items": d.get(ITEM, 0),
            "item_dedup_dropped": d.get(ITEM_DEDUP_DROPPED, 0),
            "item_failed": d.get(ITEM_FAILED, 0),
            "dedup_degraded": d.get(DEDUP_DEGRADED, 0),
            "rate": round(ok / elapsed, 3) if elapsed else 0.0,
        }

    def summary(self) -> str:
        d = self.as_dict()
        elapsed = self.elapsed()
        ok = d.get(REQUEST_OK, 0)
        rate = ok / elapsed if elapsed else 0.0
        line = (
            f"用时 {elapsed:.1f}s | 请求成功 {ok} 失败 {d.get(REQUEST_FAILED, 0)} "
            f"| 重试 {d.get(RETRY, 0)} 丢弃 {d.get(DROPPED, 0)} "
            f"| 请求去重 {d.get(DEDUP_DROPPED, 0)} | 解析异常 {d.get(PARSE_ERROR, 0)} "
            f"| 入库 {d.get(ITEM, 0)} 条（去重 {d.get(ITEM_DEDUP_DROPPED, 0)}，"
            f"失败 {d.get(ITEM_FAILED, 0)}）| {rate:.1f} 请求/s"
        )
        # 只在真的拦到过时才追加，别给没开这功能的人添噪音
        blocked = d.get(ROBOTS_DROPPED, 0)
        if blocked:
            line += f" | robots 拦截 {blocked}"
        # 去重降级过就必须说 —— 那意味着这段时间的数据可能重复入库
        degraded = d.get(DEDUP_DEGRADED, 0)
        if degraded:
            line += f" | ⚠️ 去重降级 {degraded} 批（可能重复入库）"
        return line
