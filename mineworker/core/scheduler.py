"""``AirScheduler`` —— 单进程调度器：内存队列 + 内存去重 + 本地结束检测。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mineworker.core.base_scheduler import BaseScheduler
from mineworker.core.task_queue import MemoryTaskQueue
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.network.request import Request

log = get_logger("scheduler")


class AirScheduler(BaseScheduler):
    def _make_task_queue(self) -> MemoryTaskQueue:
        return MemoryTaskQueue()

    def _is_done(self) -> bool:
        return self._local_idle() and self._task_queue.empty()

    def _on_shutdown(self) -> None:
        """有存货就 dump —— **不看是不是被中断的**。

        原来卡着 `self._interrupted`，而 `SPIDER_MAX_RUNTIME` 走的正是**不设**
        那个标志的路径（它只服务于「再按一次 Ctrl-C 强制退出」）。于是超时停止时
        一条都不 dump，实测 0 条 —— 尽管那段注释写着这条路径会 dump 未完成请求。

        这也是 0.10.2 兜底说法的落点：Redis 永久故障时请求一直留在缓冲区、
        爬虫永远不算完成、最后由超时停止 —— 恰好是不 dump 的那条路。

        正常跑完的爬虫没有存货，所以不会平白多出文件；**有存货本身就是该记一笔的信号**。
        """
        self._dump_requests(self._drain_leftovers(), "退出时仍有未完成请求")

    def _drain_leftovers(self) -> list[Request]:
        leftovers: list[Request] = [
            *self._request_buffer.drain_pending(),
            *self._collector.drain(),
        ]
        while (leftover := self._task_queue.get(timeout=0)) is not None:
            leftovers.append(leftover)
        return leftovers
