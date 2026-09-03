"""``ItemBuffer`` —— 收集 parse 产出的数据并批量交给处理函数。

阶段 02 的处理函数默认只记日志。阶段 03 会在此接入 Item / UpdateItem 与
Pipeline（按表批量 save/update、成功后写去重指纹、失败 dump）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from mineworker import setting
from mineworker.utils import stats as stats_keys
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.utils.stats import Stats

log = get_logger("item_buffer")

ItemHandler = Callable[[list[Any]], None]


def _log_handler(items: list[Any]) -> None:
    log.info("产出 {} 条数据", len(items))


class ItemBuffer(threading.Thread):
    def __init__(
        self,
        stats: Stats,
        *,
        handler: ItemHandler | None = None,
    ) -> None:
        super().__init__(name="item-buffer", daemon=True)
        self._stats = stats
        self._handler = handler or _log_handler
        self._pending: list[Any] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def put(self, item: Any) -> None:
        with self._lock:
            self._pending.append(item)
        if len(self._pending) >= setting.ITEM_MAX_CACHED_COUNT:
            self.flush()

    def is_empty(self) -> bool:
        with self._lock:
            return not self._pending

    def flush(self) -> None:
        with self._lock:
            batch = self._pending
            self._pending = []
        if not batch:
            return
        self._handler(batch)
        self._stats.incr(stats_keys.ITEM, len(batch))

    def run(self) -> None:
        while not self._stop_event.wait(setting.BUFFER_FLUSH_INTERVAL):
            self.flush()
        self.flush()

    def stop(self) -> None:
        self._stop_event.set()
