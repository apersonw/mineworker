"""AirSpider（单机）；Spider（Redis 分布式）；TaskSpider / BatchSpider（Roadmap）。"""

from __future__ import annotations

from mineworker.core.spiders.air_spider import AirSpider
from mineworker.core.spiders.spider import Spider

__all__ = ["AirSpider", "Spider"]
