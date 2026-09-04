"""把账号池接进下载中间件链：请求前挂 cookie，响应后按登录态换号重试。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from mineworker.network.middleware import DownloaderMiddleware
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.network.request import Request
    from mineworker.network.response import Response
    from mineworker.network.user_pool.base import UserPool

log = get_logger("user_pool")

CheckLogin = Callable[["Response"], bool]


class UserPoolMiddleware(DownloaderMiddleware):
    def __init__(self, pool: UserPool, *, check_login: CheckLogin | None = None) -> None:
        self._pool = pool
        self._check = check_login

    def process_request(self, request: Request) -> Request | None:
        user = self._pool.get()
        if user is None:
            log.warning("账号池没有可用账号")
            return None
        existing = request.requests_kwargs.get("cookies") or {}
        request.requests_kwargs["cookies"] = {**existing, **user.cookies}
        request.__dict__["_user"] = user
        return request

    def process_response(self, request: Request, response: Response) -> Response | Request:
        user = request.__dict__.get("_user")
        if user is None:
            return response
        if self._check is not None and not self._logged_in(response):
            log.warning("账号 {} 登录失效，拉黑并换号重试", user)
            self._pool.report_bad(user)
            retry = request.copy()
            retry.requests_kwargs.pop("cookies", None)
            retry.filter_repeat = False
            return retry
        self._pool.report_ok(user)
        return response

    def _logged_in(self, response: Response) -> bool:
        if self._check is None:
            return True
        try:
            return bool(self._check(response))
        except Exception:
            log.exception("check_login 抛异常，当作已登录处理")
            return True
