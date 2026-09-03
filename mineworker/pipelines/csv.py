"""把数据按表写到 ``<CSV_OUTPUT_DIR>/<table>.csv``。

首批数据的字段决定表头；之后出现的新字段会被忽略并记一条 warning。
"""

from __future__ import annotations

import csv
import threading
from pathlib import Path
from typing import IO, Any

from mineworker import setting
from mineworker.pipelines.base import BasePipeline
from mineworker.utils.log import get_logger

log = get_logger("pipeline.csv")


class CsvPipeline(BasePipeline):
    def __init__(self, output_dir: str | None = None) -> None:
        self._dir = Path(output_dir or setting.CSV_OUTPUT_DIR)
        self._lock = threading.Lock()
        self._files: dict[str, IO[str]] = {}
        self._writers: dict[str, csv.DictWriter[str]] = {}

    def _writer(self, table: str, sample: dict[str, Any]) -> csv.DictWriter[str]:
        writer = self._writers.get(table)
        if writer is None:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{table}.csv"
            new_file = not path.exists() or path.stat().st_size == 0
            handle = path.open("a", newline="", encoding="utf-8")
            writer = csv.DictWriter(
                handle, fieldnames=list(sample), extrasaction="ignore", restval=""
            )
            if new_file:
                writer.writeheader()
            self._files[table] = handle
            self._writers[table] = writer
        return writer

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        if not items:
            return True
        with self._lock:
            writer = self._writer(table, items[0])
            known = set(writer.fieldnames or ())
            for item in items:
                extra = set(item) - known
                if extra:
                    log.warning("[{}] 忽略新字段 {}", table, sorted(extra))
                writer.writerow(item)
            self._files[table].flush()
        return True

    def close(self) -> None:
        with self._lock:
            for handle in self._files.values():
                handle.close()
            self._files.clear()
            self._writers.clear()
