"""Item 去重不可用时，只能降级，不能带走这一批数据。

`ItemBuffer._persist()` 里有两处去重调用会走 Redis：归一化时的 `dedup.get`（查重）
和写成功后的 `dedup.add`（记指纹）。任一抛出，异常就从循环里穿出去，
后面的分组既没写库、也没 dump。

实测（9 条数据分 3 组）：
    正常              写库 9、丢失 0
    dedup.get 抖一下   写库 0、**丢失 9**
    dedup.add 抖一下   写库 3、**丢失 6**

分布式下「不销账」会让这些任务被重抓，但 AirSpider 没有租约 —— 就是永久丢失。

这一程不是靠直觉找到的，是靠 v4.23 写下的规则找到的：
「修一个网络调用时，把同一段代码里所有的网络调用一起过一遍」。
"""

from __future__ import annotations

from typing import Any

import pytest

from mineworker import Item, setting
from mineworker.buffer.item_buffer import ItemBuffer
from mineworker.pipelines.base import BasePipeline
from mineworker.utils import stats as sk
from mineworker.utils.stats import Stats


class _Pipe(BasePipeline):
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        self.rows.extend(items)
        return True


class _Dedup:
    """按调用次数抖动 —— 模拟 Redis 断连。"""

    def __init__(self, get_boom: int | None = None, add_boom: int | None = None) -> None:
        self.seen: set[str] = set()
        self.gets = 0
        self.adds = 0
        self.get_boom = get_boom
        self.add_boom = add_boom

    def get(self, fingerprint: str) -> bool:
        self.gets += 1
        if self.gets == self.get_boom:
            raise ConnectionError("Redis 断了")
        return fingerprint in self.seen

    def add(self, fingerprint: str) -> bool:
        self.adds += 1
        if self.adds == self.add_boom:
            raise ConnectionError("Redis 断了")
        self.seen.add(fingerprint)
        return True


class _A(Item):
    __table_name__ = "t0"


class _B(Item):
    __table_name__ = "t1"


class _C(Item):
    __table_name__ = "t2"


_TABLES = [_A, _B, _C]


@pytest.fixture(autouse=True)
def _on(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(setting, "ITEM_FILTER_ENABLE", True)
    monkeypatch.setattr(setting, "FAILED_ITEM_PATH", str(tmp_path / "failed_items.jsonl"))


def _run(dedup: _Dedup, n: int = 9) -> tuple[_Pipe, Stats]:
    """n 条数据分 3 组，走一次完整的 flush。"""
    pipe, stats = _Pipe(), Stats()
    buffer = ItemBuffer(stats, pipelines=["dummy"], dedup=dedup)
    buffer._pipeline_cache[("dummy",)] = [pipe]
    for i in range(n):
        item = _TABLES[i % 3]()
        item.url = f"http://example.com/p/{i}"
        buffer.put(item)
    buffer.flush()
    return pipe, stats


def test_baseline_writes_everything() -> None:
    pipe, _ = _run(_Dedup())
    assert len(pipe.rows) == 9


def test_lookup_failure_does_not_lose_the_batch() -> None:
    """查重抖一下：整批 9 条曾经全丢。"""
    pipe, stats = _run(_Dedup(get_boom=3))
    assert len(pipe.rows) == 9, f"只写进去 {len(pipe.rows)} 条，其余凭空消失"
    assert stats.get(sk.DEDUP_DEGRADED) == 1, "降级了却没有任何提示"


def test_fingerprint_failure_does_not_lose_the_rest() -> None:
    """记指纹抖一下：第一组写成功，剩下 6 条曾经凭空消失。"""
    pipe, stats = _run(_Dedup(add_boom=1))
    assert len(pipe.rows) == 9, f"只写进去 {len(pipe.rows)} 条"
    assert stats.get(sk.DEDUP_DEGRADED) == 1


def test_dedup_still_works_when_redis_is_fine() -> None:
    """别用「把去重关了」换「不丢数据」—— 正常时重复必须仍然被挡住。"""
    dedup = _Dedup()
    pipe, stats = _Pipe(), Stats()
    buffer = ItemBuffer(stats, pipelines=["dummy"], dedup=dedup)
    buffer._pipeline_cache[("dummy",)] = [pipe]
    for _ in range(3):
        item = _A()
        item.url = "http://example.com/same"
        buffer.put(item)
    buffer.flush()

    assert len(pipe.rows) == 1, f"同一条数据写进去 {len(pipe.rows)} 次"
    assert stats.get(sk.ITEM_DEDUP_DROPPED) == 2
    assert stats.get(sk.DEDUP_DEGRADED) == 0, "没出故障却报告降级"


def test_degraded_batch_is_counted_once() -> None:
    """按批记一次，别每条都刷屏。"""
    _, stats = _run(_Dedup(get_boom=2))
    assert stats.get(sk.DEDUP_DEGRADED) == 1
