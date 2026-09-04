"""进程内账号池。

``LocalUserPool``：给一批账号，轮流发放；某账号被 ``report_bad`` 后拉黑一段时间。
``GuestUserPool``：无账号，靠 ``login()`` 拿匿名 cookie，维护固定数量的游客会话。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from mineworker.network.user_pool.base import User, UserPool
from mineworker.utils.log import get_logger

log = get_logger("user_pool")

LoginFn = Callable[[User], "dict[str, str]"]


def _coerce_user(raw: User | dict[str, Any]) -> User:
    if isinstance(raw, User):
        return raw
    return User(
        username=str(raw.get("username", "guest")),
        password=str(raw.get("password", "")),
        cookies=dict(raw.get("cookies") or {}),
        extra=dict(raw.get("extra") or {}),
    )


class LocalUserPool(UserPool):
    def __init__(
        self,
        users: list[User | dict[str, Any]] | None = None,
        *,
        login: LoginFn | None = None,
    ) -> None:
        self._login = login
        self._users: list[User] = [_coerce_user(u) for u in (users or [])]
        self._blocked: dict[str, float] = {}
        self._idx = 0
        self._lock = threading.Lock()

    def add_user(self, user: User | dict[str, Any]) -> None:
        with self._lock:
            self._users.append(_coerce_user(user))

    def get(self) -> User | None:
        now = time.monotonic()
        with self._lock:
            total = len(self._users)
            for _ in range(total):
                user = self._users[self._idx % total]
                self._idx += 1
                if self._blocked.get(user.username, 0.0) > now:
                    continue
                if not user.cookies and self._login is not None:
                    try:
                        user.cookies = dict(self._login(user) or {})
                    except Exception:
                        log.exception("登录失败：{}", user.username)
                        self._blocked[user.username] = now + 300
                        continue
                return user
        return None

    def report_ok(self, user: User) -> None:
        return None

    def report_bad(self, user: User, *, block_seconds: float = 1800.0) -> None:
        with self._lock:
            self._blocked[user.username] = time.monotonic() + block_seconds
            user.cookies = {}


class GuestUserPool(LocalUserPool):
    def __init__(self, *, login: LoginFn, size: int = 3) -> None:
        super().__init__(login=login)
        self._size = max(1, size)
        self._counter = 0

    def get(self) -> User | None:
        now = time.monotonic()
        with self._lock:
            live = sum(1 for u in self._users if self._blocked.get(u.username, 0.0) <= now)
            while live < self._size:
                self._counter += 1
                self._users.append(User(username=f"guest-{self._counter}"))
                live += 1
        return super().get()
