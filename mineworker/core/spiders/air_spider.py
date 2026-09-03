"""``AirSpider`` —— 轻量单机爬虫：继承它，写 ``start_requests`` 和 ``parse`` 即可。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from mineworker import setting
from mineworker.core.base_parser import BaseParser
from mineworker.core.scheduler import AirScheduler
from mineworker.utils.log import configure as _configure_log

if TYPE_CHECKING:
    from mineworker.buffer.item_buffer import ItemHandler


class AirSpider(BaseParser):
    #: 覆写它以调整本爬虫的配置（会合并进全局 setting）
    __custom_setting__: ClassVar[dict[str, Any]] = {}

    def __init__(
        self,
        *,
        thread_count: int | None = None,
        item_handler: ItemHandler | None = None,
        pipelines: list[str] | None = None,
        debug: bool = False,
    ) -> None:
        if self.__custom_setting__:
            setting.apply(self.__custom_setting__)
        if debug:
            setting.apply({"DEBUG": True, "LOG_LEVEL": "DEBUG"})
            _configure_log()
        self._scheduler = AirScheduler(
            self,
            thread_count=thread_count,
            item_handler=item_handler,
            pipelines=pipelines,
        )

    def start(self) -> None:
        """启动爬虫，阻塞至结束。"""
        self._scheduler.run()

    def stop(self) -> None:
        """请求提前停止（优雅排空）。"""
        self._scheduler.stop()

    @property
    def scheduler(self) -> AirScheduler:
        return self._scheduler
