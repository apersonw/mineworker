"""``AirScheduler`` —— 单进程调度器：内存队列 + 内存去重 + 本地结束检测。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from mineworker import setting
from mineworker.core.base_scheduler import BaseScheduler
from mineworker.core.task_queue import MemoryTaskQueue
from mineworker.utils import tools
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
        if self._interrupted and setting.DUMP_UNFINISHED_ON_EXIT:
            self._dump_unfinished()

    def _dump_unfinished(self) -> None:
        leftovers: list[Request] = [
            *self._request_buffer.drain_pending(),
            *self._collector.drain(),
        ]
        while (leftover := self._task_queue.get(timeout=0)) is not None:
            leftovers.append(leftover)
        if not leftovers:
            return
        path = Path(setting.FAILED_REQUEST_PATH)
        with path.open("a", encoding="utf-8") as fh:
            for request in leftovers:
                fh.write(tools.dumps_json(request.to_dict()) + "\n")
        log.warning("已 dump {} 条未完成请求到 {}", len(leftovers), path)
