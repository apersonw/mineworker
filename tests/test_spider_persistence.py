"""端到端：AirSpider 通过 ITEM_PIPELINES 把数据写进（mock 的）MongoDB。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import mongomock
import pymongo
import pytest
from pytest_httpserver import HTTPServer

from mineworker import AirSpider, Item, Request, UpdateItem, setting


class NewsItem(Item):
    __unique_key__ = ["url"]


class NewsUpsertItem(UpdateItem):
    __table_name__ = "news"
    __update_key__ = ["url"]


@pytest.fixture(autouse=True)
def _fast_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "DONE_CHECK_INTERVAL", 0.04)
    monkeypatch.setattr(setting, "DONE_CHECK_TIMES", 2)
    monkeypatch.setattr(setting, "BUFFER_FLUSH_INTERVAL", 0.02)
    monkeypatch.setattr(setting, "DEDUP_FILTER", "lite")
    monkeypatch.setattr(setting, "RANDOM_USER_AGENT", False)
    monkeypatch.setattr(setting, "ITEM_PIPELINES", ["mineworker.pipelines.mongo.MongoPipeline"])
    monkeypatch.setattr(setting, "MONGO_DB", "spider_test")


@pytest.fixture
def mongo(monkeypatch: pytest.MonkeyPatch) -> mongomock.MongoClient:
    client = mongomock.MongoClient()
    monkeypatch.setattr(pymongo, "MongoClient", lambda *a, **kw: client)
    return client


def _serve(server: HTTPServer, titles: dict[str, str]) -> str:
    lis = "".join(f'<li><a href="/{k}">{v}</a></li>' for k, v in titles.items())
    server.expect_request("/list").respond_with_data(
        f"<html><body><ul>{lis}</ul></body></html>",
        content_type="text/html; charset=utf-8",
    )
    return server.url_for("/list")


def _make_spider(start_url: str, item_cls: type) -> AirSpider:
    class _S(AirSpider):
        def start_requests(self) -> Iterator[Request]:
            yield Request(start_url, callback=self.parse)

        def parse(self, request: Request, response: Any) -> Iterator[Any]:
            for li in response.css("li"):
                yield item_cls(
                    url=response.urljoin(li.css("a::attr(href)").get() or ""),
                    title=li.css("a::text").get(),
                )

    return _S()


def test_spider_writes_items_to_mongo(httpserver: HTTPServer, mongo: mongomock.MongoClient) -> None:
    url = _serve(httpserver, {"a": "A", "b": "B", "c": "C"})
    _make_spider(url, NewsItem).start()

    docs = list(mongo["spider_test"]["news"].find({}, {"_id": False}))
    assert sorted(d["title"] for d in docs) == ["A", "B", "C"]


def test_item_dedup_within_run(httpserver: HTTPServer, mongo: mongomock.MongoClient) -> None:
    # 同一列表里两个 <li> 指向同一个 url -> Item 去重后只写一条
    httpserver.expect_request("/list").respond_with_data(
        "<html><body><ul>"
        '<li><a href="/a">A</a></li><li><a href="/a">A again</a></li>'
        '<li><a href="/b">B</a></li>'
        "</ul></body></html>",
        content_type="text/html; charset=utf-8",
    )
    _make_spider(httpserver.url_for("/list"), NewsItem).start()
    assert mongo["spider_test"]["news"].count_documents({}) == 2


def test_update_item_upserts_on_rerun(httpserver: HTTPServer, mongo: mongomock.MongoClient) -> None:
    url = _serve(httpserver, {"a": "A", "b": "B"})
    _make_spider(url, NewsUpsertItem).start()
    assert mongo["spider_test"]["news"].count_documents({}) == 2

    httpserver.clear()
    _serve(httpserver, {"a": "A-updated", "b": "B", "c": "C"})
    _make_spider(httpserver.url_for("/list"), NewsUpsertItem).start()

    coll = mongo["spider_test"]["news"]
    assert coll.count_documents({}) == 3  # a、b 更新，c 新增
    assert coll.find_one({"url": httpserver.url_for("/a")})["title"] == "A-updated"
