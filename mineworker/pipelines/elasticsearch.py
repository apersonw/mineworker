"""写入 Elasticsearch。需要 ``pip install "mineworker[elasticsearch]"``。

``table_name`` 当索引名用。``save_items`` 走官方 ``helpers.bulk``；``update_items``
把 ``update_keys`` 的值拼成 ``_id`` 做 upsert（``doc_as_upsert``），这样重跑不会产生重复文档。

索引 / mapping 由你自己管（本框架不替你建索引）。
"""

from __future__ import annotations

from typing import Any

from mineworker import setting
from mineworker.pipelines.base import BasePipeline
from mineworker.utils.log import get_logger

log = get_logger("pipeline.elasticsearch")


class ElasticsearchPipeline(BasePipeline):
    def __init__(self, hosts: list[str] | None = None, *, client: Any = None) -> None:
        if client is None:
            try:
                from elasticsearch import Elasticsearch
            except ImportError as exc:  # pragma: no cover - 可选依赖
                raise ImportError(
                    'Elasticsearch 支持需 pip install "mineworker[elasticsearch]"'
                ) from exc
            client = Elasticsearch(hosts or setting.ELASTICSEARCH_HOSTS)
        self._client = client

    def _bulk(self, actions: list[dict[str, Any]], table: str, what: str) -> bool:
        try:
            from elasticsearch import helpers

            helpers.bulk(self._client, actions)
        except Exception as exc:
            log.error("[{}] {} 失败：{!r}", table, what, exc)
            return False
        return True

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        if not items:
            return True
        actions = [{"_index": table, "_source": item} for item in items]
        return self._bulk(actions, table, "bulk index")

    def update_items(self, table: str, items: list[dict[str, Any]], update_keys: list[str]) -> bool:
        if not items:
            return True
        actions: list[dict[str, Any]] = []
        for item in items:
            doc_id = "_".join(str(item.get(k, "")) for k in update_keys)
            actions.append(
                {
                    "_op_type": "update",
                    "_index": table,
                    "_id": doc_id,
                    "doc": item,
                    "doc_as_upsert": True,
                }
            )
        return self._bulk(actions, table, "bulk update")

    def close(self) -> None:
        self._client.close()
