"""把 Item 投递到 Kafka。需要 ``pip install "mineworker[kafka]"``。

``table_name`` 当 topic 用，每条 Item 序列化成一条 JSON 消息。

.. warning::
   Kafka 是**消息投递**，不是存储。这个管道只保证消息发出去了，不保证下游怎么落库，
   也**不支持 ``UpdateItem``**（消息队列没有「按主键更新一条已发出的消息」这种语义）。
   要既投递又落库，就把 Kafka 和一个数据库管道一起写进 ``ITEM_PIPELINES``。
"""

from __future__ import annotations

import json
from typing import Any

from mineworker import setting
from mineworker.pipelines.base import BasePipeline
from mineworker.utils.log import get_logger

log = get_logger("pipeline.kafka")


class KafkaPipeline(BasePipeline):
    def __init__(
        self,
        bootstrap_servers: list[str] | None = None,
        *,
        producer: Any = None,
    ) -> None:
        if producer is None:
            try:
                from kafka import KafkaProducer
            except ImportError as exc:  # pragma: no cover - 可选依赖
                raise ImportError('Kafka 支持需 pip install "mineworker[kafka]"') from exc
            producer = KafkaProducer(
                bootstrap_servers=bootstrap_servers or setting.KAFKA_BOOTSTRAP_SERVERS,
                value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode(),
            )
        self._producer = producer

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        if not items:
            return True
        try:
            for item in items:
                self._producer.send(table, item)
            # flush 才能确认这批真的发出去了；不 flush 就返回 True 等于骗上层
            self._producer.flush()
        except Exception as exc:
            log.error("[{}] 投递失败：{!r}", table, exc)
            return False
        return True

    def close(self) -> None:
        self._producer.close()
