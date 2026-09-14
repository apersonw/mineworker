"""AirSpider（单机）；Spider（Redis 分布式）；TaskSpider（任务源消费）；BatchSpider（批次采集）。"""

from __future__ import annotations

from netspy.core.spiders.air_spider import AirSpider
from netspy.core.spiders.batch_spider import BatchSpider
from netspy.core.spiders.spider import Spider
from netspy.core.spiders.task_spider import TaskSpider

__all__ = ["AirSpider", "BatchSpider", "Spider", "TaskSpider"]
