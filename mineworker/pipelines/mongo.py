"""写入 MongoDB。需要 ``pip install mineworker[mongo]``。"""

from __future__ import annotations

from typing import Any

from mineworker import setting
from mineworker.exceptions import PipelineError
from mineworker.pipelines.base import BasePipeline
from mineworker.utils.log import get_logger

log = get_logger("pipeline.mongo")


class MongoPipeline(BasePipeline):
    def __init__(
        self,
        uri: str | None = None,
        db: str | None = None,
        *,
        client: Any = None,
    ) -> None:
        if client is None:
            try:
                import pymongo
            except ImportError as exc:  # pragma: no cover - 可选依赖
                raise PipelineError("使用 MongoPipeline 需先安装 pymongo") from exc
            client = pymongo.MongoClient(uri or setting.MONGO_URI)
        self._client = client
        self._db = client[db or setting.MONGO_DB]

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        if not items:
            return True
        try:
            self._db[table].insert_many(items, ordered=False)
        except Exception as exc:  # pymongo.errors.BulkWriteError 等
            log.error("[{}] insert_many 失败：{!r}", table, exc)
            return False
        return True

    def update_items(self, table: str, items: list[dict[str, Any]], update_keys: list[str]) -> bool:
        if not items:
            return True
        collection = self._db[table]
        try:
            for item in items:
                flt = {k: item[k] for k in update_keys if k in item} or dict(item)
                collection.update_one(flt, {"$set": item}, upsert=True)
        except Exception as exc:
            log.error("[{}] update_one 失败：{!r}", table, exc)
            return False
        return True

    def close(self) -> None:
        self._client.close()
