"""ElasticsearchPipeline / KafkaPipeline —— 用替身客户端验行为。

这两个没有轻量 fake，也不值得为它们起服务容器，所以注入替身，重点验「调用形状对不对」
和「失败时返回 False 而不是静默吞掉」。
"""

from __future__ import annotations

from typing import Any

import pytest

from mineworker.pipelines.elasticsearch import ElasticsearchPipeline
from mineworker.pipelines.kafka import KafkaPipeline

ITEMS = [{"url": "https://a", "title": "一"}, {"url": "https://b", "title": "二"}]


# ---- Elasticsearch ---------------------------------------------------
class FakeES:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def captured_bulk(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """截住 helpers.bulk，拿到 actions。"""
    seen: list[Any] = []

    def fake_bulk(client: Any, actions: Any, **kw: Any) -> tuple[int, list[Any]]:
        acts = list(actions)
        seen.append(acts)
        return len(acts), []

    import elasticsearch.helpers

    monkeypatch.setattr(elasticsearch.helpers, "bulk", fake_bulk)
    return seen


def test_es_save_items_indexes_into_table(captured_bulk: list[Any]) -> None:
    assert ElasticsearchPipeline(client=FakeES()).save_items("news", ITEMS)
    actions = captured_bulk[0]
    assert [a["_index"] for a in actions] == ["news", "news"]
    assert actions[0]["_source"] == ITEMS[0]


def test_es_update_items_upserts_by_update_keys(captured_bulk: list[Any]) -> None:
    assert ElasticsearchPipeline(client=FakeES()).update_items("news", ITEMS, ["url"])
    action = captured_bulk[0][0]
    assert action["_op_type"] == "update"
    assert action["_id"] == "https://a"
    assert action["doc_as_upsert"] is True


def test_es_composite_id_joins_keys(captured_bulk: list[Any]) -> None:
    ElasticsearchPipeline(client=FakeES()).update_items("news", [{"a": 1, "b": 2}], ["a", "b"])
    assert captured_bulk[0][0]["_id"] == "1_2"


def test_es_failure_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    import elasticsearch.helpers

    def boom(client: Any, actions: Any, **kw: Any) -> None:
        raise RuntimeError("es down")

    monkeypatch.setattr(elasticsearch.helpers, "bulk", boom)
    assert ElasticsearchPipeline(client=FakeES()).save_items("news", ITEMS) is False


def test_es_empty_batch_is_noop(captured_bulk: list[Any]) -> None:
    assert ElasticsearchPipeline(client=FakeES()).save_items("news", [])
    assert captured_bulk == []


def test_es_close_delegates() -> None:
    es = FakeES()
    ElasticsearchPipeline(client=es).close()
    assert es.closed


# ---- Kafka -----------------------------------------------------------
class FakeProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.flushed = 0
        self.closed = False
        self.raise_on_send = False

    def send(self, topic: str, value: Any) -> None:
        if self.raise_on_send:
            raise RuntimeError("broker down")
        self.sent.append((topic, value))

    def flush(self) -> None:
        self.flushed += 1

    def close(self) -> None:
        self.closed = True


def test_kafka_sends_to_topic_named_after_table() -> None:
    p = FakeProducer()
    assert KafkaPipeline(producer=p).save_items("news", ITEMS)
    assert p.sent == [("news", ITEMS[0]), ("news", ITEMS[1])]


def test_kafka_flushes_before_returning_true() -> None:
    """不 flush 就返回 True 等于骗上层：消息可能还在缓冲区里没发出去。"""
    p = FakeProducer()
    KafkaPipeline(producer=p).save_items("news", ITEMS)
    assert p.flushed == 1


def test_kafka_failure_returns_false() -> None:
    p = FakeProducer()
    p.raise_on_send = True
    assert KafkaPipeline(producer=p).save_items("news", ITEMS) is False


def test_kafka_does_not_support_update_items() -> None:
    """消息队列没有「按主键更新已发出的消息」这种语义，明确不支持而不是假装成功。"""
    with pytest.raises(NotImplementedError):
        KafkaPipeline(producer=FakeProducer()).update_items("news", ITEMS, ["url"])


def test_kafka_empty_batch_is_noop() -> None:
    p = FakeProducer()
    assert KafkaPipeline(producer=p).save_items("news", [])
    assert p.sent == [] and p.flushed == 0


def test_kafka_close_delegates() -> None:
    p = FakeProducer()
    KafkaPipeline(producer=p).close()
    assert p.closed
