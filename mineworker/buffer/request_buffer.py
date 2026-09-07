"""``RequestBuffer`` —— 收集 yield 出的 Request，去重后批量写入任务队列。"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from mineworker import setting
from mineworker.dedup import get_request_filter
from mineworker.utils import stats as stats_keys
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.dedup import Filter
    from mineworker.network.request import Request
    from mineworker.utils.stats import Stats


log = get_logger("buffer")


class RequestBuffer(threading.Thread):
    def __init__(
        self,
        task_queue: Any,
        stats: Stats,
        *,
        dedup: Filter | None = None,
    ) -> None:
        super().__init__(name="request-buffer", daemon=True)
        self._queue = task_queue
        self._stats = stats
        self._dedup = dedup if dedup is not None else get_request_filter()
        self._pending: list[Request] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------

    @property
    def dedup(self) -> Filter:
        """实际在用的去重过滤器 —— 构造时传进来的可能是 None，这里给出真正生效的那个。"""
        return self._dedup

    def put(self, request: Request) -> None:
        with self._lock:
            self._pending.append(request)
        if len(self._pending) >= setting.REQUEST_BUFFER_MAX_CACHED:
            self.flush()

    def put_retry(self, request: Request) -> None:
        """重试请求：跳过去重再次入队。"""
        request.filter_repeat = False
        self.put(request)

    def is_empty(self) -> bool:
        with self._lock:
            return not self._pending

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    # ------------------------------------------------------------------
    def flush(self) -> None:
        """把缓冲区里的请求推进队列。**出错时不丢**：没入队的放回去下轮重试。

        原来是「先把整批从 `_pending` 摘走，再逐个 put」—— 队列一次抖动
        （Redis 断连 / OOM / 网络闪断），剩下的请求既不在 `_pending`、
        也没进队列、也没像 Item 那样 dump 到文件，**静默消失**。
        实测 10 个请求、第 3 个上抖一次：丢 8 个。

        放回去还有个坑：`dedup.add()` 是在入队**之前**写指纹的，所以重试时
        会被自己刚写下的那条指纹挡掉、当成重复丢弃 —— 等于没救回来。
        因此放回的请求要清掉 `filter_repeat`：它们**已经过过去重**了。
        """
        with self._lock:
            batch = self._pending
            self._pending = []
        for i, request in enumerate(batch):
            if request.filter_repeat and not self._dedup.add(request.fingerprint):
                self._stats.incr(stats_keys.DEDUP_DROPPED)
                continue
            try:
                self._queue.put(request)
            except Exception:
                # 这一条和它后面的都还没入队，整体放回缓冲区
                self._requeue(batch[i:])
                raise

    def _requeue(self, requests: list[Request]) -> None:
        """把没能入队的请求放回缓冲区头部，保持原有顺序（优先级靠队列自己排）。"""
        for request in requests:
            # 已经过过去重了；不清掉的话下一轮会被自己刚写的指纹挡掉
            request.filter_repeat = False
        with self._lock:
            self._pending[:0] = requests

    def drain_pending(self) -> list[Request]:
        with self._lock:
            batch = self._pending
            self._pending = []
        return batch

    def run(self) -> None:
        while not self._stop_event.wait(setting.BUFFER_FLUSH_INTERVAL):
            self._flush_guarded()
        self._flush_guarded()

    def _flush_guarded(self) -> None:
        """后台线程里的 flush 必须兜住异常。

        原来是裸调 `flush()` —— 队列一次抖动就把整个缓冲区线程打死，此后**所有**
        请求都不再进队列，爬虫静默停止发现新页面（实测线程存活 False）。

        但也不能静默吞掉：不留日志就只是把一种静默换成另一种。请求已经由
        `flush` 放回缓冲区，下一轮自然重试。
        """
        try:
            self.flush()
        except Exception:
            log.exception("请求入队失败，{} 条留在缓冲区等下轮重试", len(self._pending))

    def stop(self) -> None:
        self._stop_event.set()
