"""代理池接口。"""

from __future__ import annotations

import abc


class ProxyPool(abc.ABC):
    @abc.abstractmethod
    def get_proxy(self) -> str | None:
        """返回一个可用代理（形如 ``http://host:port``），无则返回 None。"""

    def report_bad(self, proxy: str) -> None:  # noqa: B027 - 可选钩子
        """标记某代理不可用。"""

    def close(self) -> None:  # noqa: B027 - 可选钩子
        """释放资源。"""
