"""AirSpider（单机）；Spider（Redis 分布式）；TaskSpider（任务源消费）。"""

from __future__ import annotations

from mineworker.core.spiders.air_spider import AirSpider
from mineworker.core.spiders.spider import Spider
from mineworker.core.spiders.task_spider import TaskSpider

__all__ = ["AirSpider", "Spider", "TaskSpider"]
