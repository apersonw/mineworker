"""账号 / Cookie 池接口。

一个 :class:`User` 就是「一份可用的会话」：可能带账号密码，一定带（或延迟登录后带）
cookies。:class:`UserPool` 负责发放、回收、以及把失效的账号拉黑一段时间。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class User:
    username: str = "guest"
    password: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.username


class UserPool(abc.ABC):
    @abc.abstractmethod
    def get(self) -> User | None:
        """借一个可用账号；没有可用的返回 None。"""

    def report_ok(self, user: User) -> None:  # noqa: B027
        """账号用完且正常，归还池子。"""

    def report_bad(self, user: User, *, block_seconds: float = 1800.0) -> None:  # noqa: B027
        """账号被封 / cookie 失效，拉黑 ``block_seconds`` 秒。"""

    def close(self) -> None:  # noqa: B027
        """释放资源。"""
