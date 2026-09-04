"""``TaskSpider`` —— 从任务源（默认 Redis list）持续拉任务来爬。

适合「有一堆待抓的 id / url，想用一个或多个常驻进程慢慢消费」的场景。
生产者 ``TaskSpider.push_tasks(...)``（或运行中 ``self.add_tasks(...)``）；
消费者在一台或多台机器上 ``MySpider().start()``。需要 ``pip install mineworker[redis]``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from mineworker import setting
from mineworker.core.spiders.spider import Spider
from mineworker.utils import tools

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mineworker.buffer.item_buffer import ItemHandler
    from mineworker.core.redis_task_scheduler import RedisTaskScheduler
    from mineworker.network.request import Request


def _task_source_key(name: str) -> str:
    return f"{setting.REDIS_KEY_PREFIX}:{name}:tasks"


class TaskSpider(Spider):
    __custom_setting__: ClassVar[dict[str, Any]] = {}

    def __init__(
        self,
        *,
        redis_key: str | None = None,
        task_key: str | None = None,
        keep_alive: bool | None = None,
        thread_count: int | None = None,
        item_handler: ItemHandler | None = None,
        pipelines: list[str] | None = None,
    ) -> None:
        if self.__custom_setting__:
            setting.apply(self.__custom_setting__)
        try:
            from mineworker.core.redis_task_scheduler import RedisTaskScheduler
            from mineworker.core.task_source import RedisTaskSource
            from mineworker.db.redisdb import get_redis
        except ImportError as exc:  # pragma: no cover
            raise ImportError("TaskSpider 需要 Redis：pip install mineworker[redis]") from exc

        rk = redis_key or type(self).__name__
        self._task_source = RedisTaskSource(get_redis(), _task_source_key(task_key or rk))
        self._scheduler: RedisTaskScheduler = RedisTaskScheduler(
            self,
            redis_key=rk,
            keep_alive=keep_alive,
            thread_count=thread_count,
            item_handler=item_handler,
            pipelines=pipelines,
            fetch_tasks=self.fetch_tasks,
            task_requests=self.task_requests,
        )

    # ------------------------------------------------------------------
    # 用户覆写
    # ------------------------------------------------------------------
    def task_requests(self, task: Any) -> Iterable[Request]:
        """把一个任务（dict）变成一个或多个 Request。必须实现。"""
        raise NotImplementedError(f"{type(self).__name__} 需实现 task_requests(self, task)")

    def fetch_tasks(self, limit: int) -> list[Any]:
        """拉一批任务。默认从 Redis list 取；覆写以从 MySQL / Mongo 查。"""
        return [tools.loads_json(raw) for raw in self._task_source.fetch(limit)]

    # ------------------------------------------------------------------
    def add_tasks(self, *tasks: Any) -> None:
        """运行中追加任务（比如在 parse 里发现了新的待抓项）。"""
        self._task_source.push(*(tools.dumps_json(t) for t in tasks))

    @classmethod
    def push_tasks(cls, *tasks: Any, task_key: str | None = None) -> None:
        """生产者用：把任务塞进 Redis 任务源。"""
        from mineworker.core.task_source import RedisTaskSource
        from mineworker.db.redisdb import get_redis

        source = RedisTaskSource(get_redis(), _task_source_key(task_key or cls.__name__))
        source.push(*(tools.dumps_json(t) for t in tasks))

    def start(self) -> None:
        self._scheduler.run()

    def stop(self) -> None:
        self._scheduler.stop()

    @property
    def scheduler(self) -> RedisTaskScheduler:
        return self._scheduler
