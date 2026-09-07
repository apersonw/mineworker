"""存储适配：MongoDB（管道，见 pipelines/）；Redis（分布式队列 / 去重 / 锁）。

这里的名字**惰性导出**：`redisdb` 顶层 ``import redis``，而 redis 是可选 extra。
写成普通的顶层 import 会让 ``from mineworker.db.mysqldb import MysqlDB`` 也跟着失败 ——
导一个子模块不该把父包的可选依赖一起拖进来。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mineworker.db.redisdb import acquire_once, close_redis, get_redis, key

__all__ = ["acquire_once", "close_redis", "get_redis", "key"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from mineworker.db import redisdb

        return getattr(redisdb, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
