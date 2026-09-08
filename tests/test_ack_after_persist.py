"""销账必须晚于落库。

实测（400 页、跑到一半 SIGKILL、第二个节点接手跑完）：管道每批写入 1.5 秒时，
靶子发出 400 页、爬虫 exitcode=0 报告成功，**只落库了 340 行**，
少掉的 60 行重跑一遍一行都补不回来 —— 请求指纹是入队前就写的。

根因是顺序：worker 在 `finally` 里销账，那一刻 item 还只在内存缓冲里。
销账 = 释放租约 = 宣布「这条干完了」，于是节点一死，数据没了、任务也不会被回收。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from mineworker import setting
from mineworker.buffer.item_buffer import ItemBuffer
from mineworker.dedup import Dedup
from mineworker.pipelines.base import BasePipeline
from mineworker.utils.stats import Stats


class RefusingPipeline(BasePipeline):
    """写入失败（返回 False）—— 这批会被 dump 到 failed_items。"""

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        return False


class SlowPipeline(BasePipeline):
    """模拟真实的库：批量写入要花时间（MySQL 批插几百行 ~1 秒很常见）。"""

    gate = threading.Event()
    saved: list[dict[str, Any]] = []

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        SlowPipeline.gate.wait(timeout=5)
        SlowPipeline.saved.extend(items)
        return True


_PIPE = f"{__name__}.SlowPipeline"


@pytest.fixture(autouse=True)
def _reset() -> None:
    SlowPipeline.gate = threading.Event()
    SlowPipeline.saved = []


def _buffer(acked: list[Any], **kw: Any) -> ItemBuffer:
    kw.setdefault("pipelines", [_PIPE])
    kw.setdefault("dedup", Dedup(filter_type="lite"))
    return ItemBuffer(Stats(), ack=acked.append, **kw)


def test_no_ack_while_data_is_still_only_in_memory() -> None:
    """落库没完成之前不许销账 —— 这就是那 60 行丢掉的地方。"""
    acked: list[Any] = []
    buf = _buffer(acked)
    buf.put({"url": "/p/1"}, owner="req-1")

    t = threading.Thread(target=buf.flush, daemon=True)
    t.start()
    time.sleep(0.3)  # 卡在管道里：数据既不在缓冲区、也还没进库
    assert acked == [], "数据还没落库就把任务销账了 —— 此刻硬杀，这条数据永远丢失"

    SlowPipeline.gate.set()
    t.join(timeout=5)
    assert acked == ["req-1"]
    assert SlowPipeline.saved == [{"url": "/p/1"}]


def test_one_request_many_items_acks_once() -> None:
    acked: list[Any] = []
    buf = _buffer(acked)
    for i in range(5):
        buf.put({"url": f"/p/{i}"}, owner="req-1")
    SlowPipeline.gate.set()
    buf.flush()
    assert acked == ["req-1"], "一条请求产出 5 条数据，账只该销一次"


def test_ack_is_skipped_when_persisting_blows_up() -> None:
    """落库整个抛穿时不能销账：任务留在在途表里，租约到期后还有第二次机会。

    管道自己抛异常是被 `_write` 兜住的（返回 False → dump），穿不出来。
    真正会穿透 `_persist` 的是归一化失败 —— 比如 yield 了个既不是 Item
    也不是 dict 的东西。
    """
    acked: list[Any] = []
    buf = _buffer(acked)
    SlowPipeline.gate.set()
    buf.put(object(), owner="req-1")  # 无法归一化
    buf.flush()  # 而且不许把 ItemBuffer 线程带走
    assert acked == [], "落库抛穿了却销账 —— 数据和任务一起没了"


def test_failed_write_still_acks_because_dump_is_durable(tmp_path: Any) -> None:
    """管道返回 False 时数据会 dump 到 failed_items —— 那也是持久介质，可以销账。"""
    acked: list[Any] = []
    dump = tmp_path / "failed.jsonl"
    old = setting.FAILED_ITEM_PATH
    setting.FAILED_ITEM_PATH = str(dump)
    try:
        buf = _buffer(acked, pipelines=[f"{__name__}.RefusingPipeline"])
        buf.put({"url": "/p/1"}, owner="req-1")
        buf.flush()
    finally:
        setting.FAILED_ITEM_PATH = old
    assert dump.exists() and dump.read_text().strip(), "没 dump 就销账等于丢数据"
    assert acked == ["req-1"]


def test_items_without_owner_need_no_ack() -> None:
    """AirSpider / 调试场景没有任务队列，owner 为空时不该炸。"""
    acked: list[Any] = []
    buf = _buffer(acked)
    buf.put({"url": "/p/1"})
    SlowPipeline.gate.set()
    buf.flush()
    assert acked == []
    assert SlowPipeline.saved == [{"url": "/p/1"}]


def test_handler_fast_path_also_acks() -> None:
    """调试快路径（handler）不落库，但任务照样要销账，否则节点永远收不了工。"""
    acked: list[Any] = []
    seen: list[Any] = []
    buf = ItemBuffer(Stats(), handler=seen.extend, ack=acked.append)
    buf.put({"url": "/p/1"}, owner="req-1")
    buf.flush()
    assert seen == [{"url": "/p/1"}]
    assert acked == ["req-1"]


def test_request_stays_held_until_its_data_lands() -> None:
    """阶段 B：等落库的请求必须仍算「本节点持有」。

    不然上一程刚修好的误判会在新位置原样复现 —— 而慢管道下这个状态等得最久，
    最容易被别的节点当成「死了」抢走重抓。结束判定同样得认它，
    否则节点会在数据还没落库时就宣布抓完、退出。

    这条性质是白捡的：`done()` 本来就是把请求从 `_in_progress` 里摘掉的地方，
    而续期和结束判定读的正是它。但「本该白捡」和「真的接上了」是两回事。
    """
    from mineworker.buffer.request_buffer import RequestBuffer
    from mineworker.core.base_parser import BaseParser
    from mineworker.core.collector import Collector
    from mineworker.core.parser_control import ParserWorker
    from mineworker.network.request import Request

    class _Queue:
        def __init__(self) -> None:
            self.handed: list[Request] = []
            self.acked: list[Request] = []

        def get(self, timeout: float = 1.0) -> Request | None:
            return self.handed.pop(0) if self.handed else None

        def get_batch(self, n: int) -> list[Request]:
            return []

        def done(self, request: Request) -> None:
            self.acked.append(request)

        def put(self, request: Request) -> None:
            return None

        def empty(self) -> bool:
            return not self.handed

    queue = _Queue()
    collector = Collector(queue)
    req = Request("http://example.com/p/1")
    req.callback = "parse"
    queue.handed.append(req)

    buf = _buffer([])  # ack 先留空，等下换成 collector.done
    buf._ack = collector.done
    worker = ParserWorker(
        0,
        parser=BaseParser(),
        collector=collector,
        request_buffer=RequestBuffer(queue, Stats(), dedup=Dedup(filter_type="lite")),
        item_buffer=buf,
        stats=Stats(),
    )
    # 不真下载：直接走分发这一段 —— 要验的是「产出了 item 之后销不销账」
    worker._process = lambda request: worker._dispatch(  # type: ignore[method-assign]
        [{"url": request.url}], request
    )
    worker.start()
    try:
        deadline = time.time() + 5
        while not buf.pending_count() and time.time() < deadline:
            time.sleep(0.02)
        assert buf.pending_count() == 1, "数据没进缓冲，这个用例什么都没验到"
        time.sleep(0.2)  # 给 worker 充分的时间走完 finally
        assert req in collector.held_requests(), (
            "数据还没落库，请求就不算持有了 —— 租约不会再续，会被别的节点抢走重抓"
        )
        assert queue.acked == [], "数据还没落库就销账了"

        SlowPipeline.gate.set()
        buf.flush()
        assert queue.acked == [req], "落库了却没销账 —— 任务会被白白重抓一遍"
        assert req not in collector.held_requests()
    finally:
        worker.stop()
        SlowPipeline.gate.set()
