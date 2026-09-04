"""``BaseParser`` —— 用户爬虫要继承并覆写的基类。

只有 :meth:`start_requests` 和 :meth:`parse`（或每个 Request 指定的 ``callback``）
是必须关心的，其余都有合理默认值。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mineworker.network.request import Request
    from mineworker.network.response import Response
    from mineworker.network.user_pool.base import UserPool

#: parse / callback 允许 yield 的类型：新的 Request、数据对象、或待执行的可调用
ParseResult = Iterable[Any]


class BaseParser:
    # ------------------------------------------------------------------
    # 必须 / 常用
    # ------------------------------------------------------------------
    def start_requests(self) -> Iterable[Request]:
        """产出种子请求。"""
        return ()

    def parse(self, request: Request, response: Response) -> ParseResult | None:
        """默认回调。未给 Request 指定 callback 时调用。"""
        raise NotImplementedError(
            f"{type(self).__name__} 需要实现 parse()，或为每个 Request 指定 callback"
        )

    # ------------------------------------------------------------------
    # 钩子（可选覆写）
    # ------------------------------------------------------------------
    def download_midware(self, request: Request) -> Request | None:
        """下载前的最后一次修改机会。返回 Request 以替换，返回 None 用原请求。"""
        return None

    def validate(self, request: Request, response: Response) -> bool | None:
        """校验响应。返回 False 丢弃；抛 ValidationError 触发重试；抛 NotRetryError 丢弃。"""
        return True

    def failed_request(self, request: Request, response: Response | None) -> ParseResult | None:
        """重试耗尽后的兜底。可再 yield Request / 数据。"""
        return None

    def exception_request(
        self, request: Request, response: Response | None, exception: BaseException
    ) -> ParseResult | None:
        """下载 / 解析抛异常且仍会重试时调用（观测用，通常不 yield）。"""
        return None

    # ------------------------------------------------------------------
    # 账号 / Cookie 池（可选）
    # ------------------------------------------------------------------
    def user_pool(self) -> UserPool | None:
        """返回一个账号池，返回 None 表示不用。调度器会自动挂到下载链上。"""
        return None

    def check_login(self, response: Response) -> bool:
        """用了账号池时判断响应是否处于登录态。返回 False -> 拉黑当前账号并换号重试。"""
        return True

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start_callback(self) -> None:
        """爬虫启动时调用一次。"""

    def end_callback(self) -> None:
        """爬虫正常结束时调用一次。"""
