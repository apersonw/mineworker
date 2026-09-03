"""``ItemBuffer`` —— 收集 parse 产出的数据，批量去重后交给管道落库。

流程：``put`` 累积 → 定时 / 满量 ``flush`` → 按 (表, 是否 UpdateItem, 管道) 分组
→ Item 级去重（fingerprint）→ 逐管道 ``save_items`` / ``update_items``
→ 成功则写去重指纹；失败则 dump 到 ``FAILED_ITEM_PATH``。

给了 ``handler`` 时走调试快路径：直接把原始批次交给 handler，不去重、不落库。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from mineworker import setting
from mineworker.exceptions import ItemError
from mineworker.network.item import Item, UpdateItem
from mineworker.utils import stats as sk
from mineworker.utils import tools
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.dedup import Dedup
    from mineworker.pipelines.base import BasePipeline
    from mineworker.utils.stats import Stats

log = get_logger("item_buffer")

ItemHandler = Callable[[list[Any]], None]


class _Norm(NamedTuple):
    table: str
    is_update: bool
    data: dict[str, Any]
    fingerprint: str | None
    update_keys: tuple[str, ...]
    pipelines: tuple[str, ...] | None


def _normalize(obj: Any) -> _Norm:
    if isinstance(obj, Item):
        obj.pre_to_db()
        is_update = isinstance(obj, UpdateItem)
        keys = tuple(obj.update_key) if isinstance(obj, UpdateItem) else ()
        fp = obj.fingerprint if setting.ITEM_FILTER_ENABLE else None
        pipes = tuple(obj.pipelines) if obj.pipelines else None
        return _Norm(obj.table_name, is_update, obj.to_dict(), fp, keys, pipes)
    if isinstance(obj, dict):
        return _Norm(setting.ITEM_DEFAULT_TABLE, False, obj, None, (), None)
    raise ItemError(f"无法入库的类型：{type(obj)!r}（需要 Item 或 dict）")


class ItemBuffer(threading.Thread):
    def __init__(
        self,
        stats: Stats,
        *,
        handler: ItemHandler | None = None,
        pipelines: list[str] | None = None,
        dedup: Dedup | None = None,
    ) -> None:
        super().__init__(name="item-buffer", daemon=True)
        self._stats = stats
        self._handler = handler
        self._pipeline_paths = pipelines
        self._pipeline_cache: dict[tuple[str, ...], list[BasePipeline]] = {}
        self._dedup = dedup
        self._pending: list[Any] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    def put(self, item: Any) -> None:
        with self._lock:
            self._pending.append(item)
            size = len(self._pending)
        if size >= setting.ITEM_MAX_CACHED_COUNT:
            self.flush()

    def is_empty(self) -> bool:
        with self._lock:
            return not self._pending

    def run(self) -> None:
        while not self._stop_event.wait(setting.BUFFER_FLUSH_INTERVAL):
            self.flush()
        self.flush()

    def stop(self) -> None:
        self._stop_event.set()

    def close(self) -> None:
        for pipelines in self._pipeline_cache.values():
            for pipeline in pipelines:
                try:
                    pipeline.close()
                except Exception:
                    log.exception("管道 {} close 异常", type(pipeline).__name__)
        self._pipeline_cache.clear()

    # ------------------------------------------------------------------
    def flush(self) -> None:
        with self._lock:
            batch = self._pending
            self._pending = []
        if not batch:
            return
        if self._handler is not None:
            self._handler(batch)
            self._stats.incr(sk.ITEM, len(batch))
            return
        self._persist(batch)

    def _persist(self, batch: list[Any]) -> None:
        dedup = self._get_dedup()
        seen: set[str] = set()
        groups: dict[tuple[str, bool, tuple[str, ...] | None], list[_Norm]] = defaultdict(list)
        for obj in batch:
            norm = _normalize(obj)
            if norm.fingerprint is not None and dedup is not None:
                if norm.fingerprint in seen or dedup.get(norm.fingerprint):
                    self._stats.incr(sk.ITEM_DEDUP_DROPPED)
                    continue
                seen.add(norm.fingerprint)
            groups[(norm.table, norm.is_update, norm.pipelines)].append(norm)

        for (table, is_update, pipe_paths), rows in groups.items():
            datas = [row.data for row in rows]
            update_keys = list(rows[0].update_keys)
            pipelines = self._resolve_pipelines(pipe_paths)
            ok = all(
                self._write(pipeline, table, is_update, datas, update_keys)
                for pipeline in pipelines
            )
            if ok:
                self._stats.incr(sk.ITEM, len(datas))
                if dedup is not None:
                    for row in rows:
                        if row.fingerprint is not None:
                            dedup.add(row.fingerprint)
            else:
                self._dump_failed(table, datas)

    # ------------------------------------------------------------------
    def _get_dedup(self) -> Dedup | None:
        if not setting.ITEM_FILTER_ENABLE:
            return None
        if self._dedup is None:
            from mineworker.dedup import get_item_filter

            self._dedup = get_item_filter()
        return self._dedup

    def _resolve_pipelines(self, paths: tuple[str, ...] | None) -> list[BasePipeline]:
        key = (
            paths
            if paths is not None
            else tuple(
                self._pipeline_paths if self._pipeline_paths is not None else setting.ITEM_PIPELINES
            )
        )
        cached = self._pipeline_cache.get(key)
        if cached is None:
            cached = [tools.load_object(path)() for path in key]
            self._pipeline_cache[key] = cached
        return cached

    def _write(
        self,
        pipeline: BasePipeline,
        table: str,
        is_update: bool,
        datas: list[dict[str, Any]],
        update_keys: list[str],
    ) -> bool:
        try:
            if is_update:
                return pipeline.update_items(table, datas, update_keys)
            return pipeline.save_items(table, datas)
        except Exception:
            log.exception("管道 {} 写入异常", type(pipeline).__name__)
            return False

    def _dump_failed(self, table: str, datas: list[dict[str, Any]]) -> None:
        path = Path(setting.FAILED_ITEM_PATH)
        with path.open("a", encoding="utf-8") as fh:
            for data in datas:
                fh.write(tools.dumps_json({"table": table, "data": data}) + "\n")
        self._stats.incr(sk.ITEM_FAILED, len(datas))
        log.error("[{}] {} 条数据写入失败，已 dump 到 {}", table, len(datas), path)
