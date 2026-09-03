"""把数据打到日志，用于调试。"""

from __future__ import annotations

from typing import Any

from mineworker.pipelines.base import BasePipeline
from mineworker.utils import tools
from mineworker.utils.log import get_logger

log = get_logger("pipeline.console")


class ConsolePipeline(BasePipeline):
    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        for item in items:
            log.info("[{}] {}", table, tools.dumps_json(item))
        return True

    def update_items(self, table: str, items: list[dict[str, Any]], update_keys: list[str]) -> bool:
        for item in items:
            log.info("[{}] upsert(by={}) {}", table, update_keys, tools.dumps_json(item))
        return True
