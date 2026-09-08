"""批次任务标「已完成」不能早于数据落库。

文档推荐的写法是：

    def parse(self, request, response, task):
        yield {"url": ..., "title": ...}          # item 进内存缓冲
        self.update_task(task["id"], ok=True)     # 立刻标 DONE

真 MySQL 实测（5 个任务照这个写法处理完、缓冲区未 flush）：任务表 **DONE 5 个**，
实际落库 **0 行**。而 `reset_lost_tasks` 只回收「处理中」—— 标了 DONE 的任务
永远不会被重跑，批次报告 100% 完成而数据一行没有。

这是 v4.14「销账早于落库」在批次任务表上的化身：v4.14 修的是请求队列的销账，
批次状态是另一个状态存储，同一个洞原样还在。
"""

from __future__ import annotations

from typing import Any

import pytest

from mineworker.buffer.item_buffer import ItemBuffer
from mineworker.core import context
from mineworker.core.batch_store import MemoryBatchStore
from mineworker.core.spiders.batch_spider import BatchSpider
from mineworker.dedup import Dedup
from mineworker.network.request import Request
from mineworker.pipelines.base import BasePipeline
from mineworker.utils.stats import Stats


class _Pipeline(BasePipeline):
    rows: list[dict[str, Any]] = []

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        _Pipeline.rows.extend(items)
        return True


class _Refusing(BasePipeline):
    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        return False


def _spider(store: Any) -> Any:
    """只借 update_task 这一个方法 —— 起整个 BatchSpider 要连 Redis 和 MySQL。"""
    obj = type("_S", (), {})()
    obj._store = store
    obj.update_task = BatchSpider.update_task.__get__(obj)
    return obj


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    _Pipeline.rows = []
    from mineworker import setting

    monkeypatch.setattr(setting, "ITEM_FILTER_ENABLE", False)
    # 别把 dump 写进仓库根目录
    monkeypatch.setattr(setting, "FAILED_ITEM_PATH", str(tmp_path / "failed_items.jsonl"))
    yield
    context.set_current(None, None)


def _buffer(pipeline: str) -> ItemBuffer:
    return ItemBuffer(Stats(), pipelines=[pipeline], dedup=Dedup(filter_type="lite"))


def test_done_waits_for_the_data_to_land() -> None:
    store = MemoryBatchStore([{"id": 1}])
    store.claim_tasks(1)
    buf = _buffer(f"{__name__}._Pipeline")
    spider = _spider(store)
    request = Request("http://example.com/p/1")

    context.set_current(request, buf)
    buf.put({"url": "http://example.com/p/1"}, owner=request)
    spider.update_task(1, ok=True)

    assert store.count_tasks().done == 0, (
        "数据还在内存缓冲里，任务就被标成了已完成 —— "
        "节点这时一死，防丢机制只回收「处理中」，这个任务永远不会被重跑"
    )
    buf.flush()
    assert store.count_tasks().done == 1, "落库了却没标 DONE —— 任务会被无限重跑"
    assert len(_Pipeline.rows) == 1


def test_task_without_items_is_marked_immediately() -> None:
    """没产出 item 的请求要立刻标 —— 否则永远等不到落库那一刻。"""
    store = MemoryBatchStore([{"id": 1}])
    store.claim_tasks(1)
    buf = _buffer(f"{__name__}._Pipeline")
    spider = _spider(store)

    context.set_current(Request("http://example.com/p/1"), buf)
    spider.update_task(1, ok=True)
    assert store.count_tasks().done == 1


def test_marking_failed_is_never_deferred() -> None:
    """标失败不取决于数据落没落库。"""
    store = MemoryBatchStore([{"id": 1}])
    store.claim_tasks(1)
    buf = _buffer(f"{__name__}._Pipeline")
    spider = _spider(store)
    request = Request("http://example.com/p/1")

    context.set_current(request, buf)
    buf.put({"url": "x"}, owner=request)
    spider.update_task(1, ok=False)
    assert store.count_tasks().failed == 1


def test_no_request_context_marks_immediately() -> None:
    """master 那边不在请求上下文里调用，不能因此卡住。"""
    store = MemoryBatchStore([{"id": 1}])
    store.claim_tasks(1)
    spider = _spider(store)
    spider.update_task(1, ok=True)
    assert store.count_tasks().done == 1


def test_dumped_write_still_marks_the_task_done() -> None:
    """写库失败的数据会 dump 到 `failed_items.jsonl` —— 那也是持久介质，任务照样算完成。

    这条沿用 v4.14 定下的规则（「dump 也算落到了持久介质，可以销账」）。
    两套机制必须对「完成」用同一个定义，否则请求队列已经销账、批次任务却还挂着，
    谁也说不清这个批次到底跑没跑完。

    代价是批次报告完成时，有 N 行躺在 dump 文件里而不是库里 ——
    换来的是不必为「库抖了一下」把整页重抓一遍。框架会记 error，
    `mineworker retry --items` 可回放。
    """
    store = MemoryBatchStore([{"id": 1}])
    store.claim_tasks(1)
    buf = _buffer(f"{__name__}._Refusing")
    spider = _spider(store)
    request = Request("http://example.com/p/1")

    context.set_current(request, buf)
    buf.put({"url": "x"}, owner=request)
    spider.update_task(1, ok=True)
    buf.flush()
    assert store.count_tasks().done == 1


def test_worker_actually_sets_the_context() -> None:
    """上面几条都是自己 set_current 之后测机制 —— 那验不到「worker 有没有接上」。

    第一次反向验证就栽在这里：把 worker 里设置上下文那行破坏掉，5 条用例照样全绿。
    这条走真实的 AirSpider 路径，从用户回调里面回头看上下文。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import mineworker as mw
    from mineworker import setting
    from mineworker.utils import log

    class _T(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            return None

        def do_GET(self) -> None:
            body = b"<html><body><h1>ok</h1></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _T)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    seen: list[tuple[Any, Any]] = []

    setting.LOG_LEVEL = "CRITICAL"
    setting.SPIDER_THREAD_COUNT = 1
    setting.ROBOTS_OBEY = False
    setting.ITEM_PIPELINES = []
    log.configure()

    class _Spider(mw.AirSpider):
        def start_requests(self) -> Any:
            yield mw.Request(f"http://127.0.0.1:{port}/p/1", callback=self.parse_page)

        def parse_page(self, request: Any, response: Any) -> Any:
            seen.append((context.get_current_request(), context.get_current_item_buffer()))
            return None

    try:
        _Spider().start()
    finally:
        server.shutdown()

    assert seen, "回调没被调用，这个用例什么都没验到"
    current_request, current_buffer = seen[0]
    assert current_request is not None, "worker 没有设置当前请求上下文 —— update_task 会退回立刻写"
    assert current_request.url.endswith("/p/1")
    assert current_buffer is not None, "上下文里没有 item 缓冲，无法把标 DONE 推迟到落库之后"
