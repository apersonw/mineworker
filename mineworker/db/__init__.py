"""存储适配：MongoDB（管道，见 pipelines/）；Redis（分布式队列 / 去重 / 锁）。"""

from __future__ import annotations

from mineworker.db.redisdb import acquire_once, close_redis, get_redis, key

__all__ = ["acquire_once", "close_redis", "get_redis", "key"]
