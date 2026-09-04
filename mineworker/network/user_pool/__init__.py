"""账号 / Cookie 池（v2.4）。

- :class:`LocalUserPool` / :class:`GuestUserPool`  进程内
- :class:`RedisUserPool`                          多节点共享
- :class:`UserPoolMiddleware`                     接进下载链（Scheduler 自动挂载）

爬虫里覆写 ``user_pool()`` 返回一个 pool，覆写 ``check_login(response)`` 判断登录态即可。
"""

from __future__ import annotations

from mineworker.network.user_pool.base import User, UserPool
from mineworker.network.user_pool.local import GuestUserPool, LocalUserPool
from mineworker.network.user_pool.middleware import UserPoolMiddleware
from mineworker.network.user_pool.redis import RedisUserPool

__all__ = [
    "GuestUserPool",
    "LocalUserPool",
    "RedisUserPool",
    "User",
    "UserPool",
    "UserPoolMiddleware",
]
