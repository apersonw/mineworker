"""``Spider`` —— 分布式爬虫：队列 / 去重都在 Redis，可多进程 / 多机跑同一个爬虫。

用法和 ``AirSpider`` 一样，只是换个基类。多个进程用同一个 ``redis_key``（默认类名）
即共享队列与去重，支持断点续爬。需要 ``pip install mineworker[redis]``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from mineworker import setting
from mineworker.core.base_parser import BaseParser

if TYPE_CHECKING:
    from mineworker.buffer.item_buffer import ItemHandler
    from mineworker.core.redis_scheduler import RedisScheduler


class Spider(BaseParser):
    __custom_setting__: ClassVar[dict[str, Any]] = {}

    def __init__(
        self,
        *,
        redis_key: str | None = None,
        thread_count: int | None = None,
        keep_alive: bool | None = None,
        item_handler: ItemHandler | None = None,
        pipelines: list[str] | None = None,
    ) -> None:
        if self.__custom_setting__:
            setting.apply(self.__custom_setting__)
        try:
            from mineworker.core.redis_scheduler import RedisScheduler
        except ImportError as exc:  # pragma: no cover - 缺 redis
            raise ImportError("Spider 需要 Redis：pip install mineworker[redis]") from exc
        self._scheduler: RedisScheduler = RedisScheduler(
            self,
            redis_key=redis_key or type(self).__name__,
            keep_alive=keep_alive,
            thread_count=thread_count,
            item_handler=item_handler,
            pipelines=pipelines,
        )

    def start(self) -> None:
        """启动爬虫。``keep_alive=False`` 时爬完退出；否则常驻轮询队列。"""
        self._scheduler.run()

    def stop(self) -> None:
        self._scheduler.stop()

    @property
    def scheduler(self) -> RedisScheduler:
        return self._scheduler
