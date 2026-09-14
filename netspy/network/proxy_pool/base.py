"""代理池接口。"""

from __future__ import annotations

import abc


class ProxyPool(abc.ABC):
    @abc.abstractmethod
    def get_proxy(self) -> str | None:
        """返回一个可用代理（形如 ``http://host:port``），无则返回 None。"""

    def report_bad(self, proxy: str) -> None:  # noqa: B027 - 可选钩子
        """标记某代理不可用 —— **确定是代理自己的错**（连不上、CONNECT 被拒）。"""

    def report_suspect(self, proxy: str) -> None:  # noqa: B027 - 可选钩子
        """一次说不清是谁的错的失败（读超时、连接重置、响应畸形）。

        默认不作为 —— 自定义代理池不实现它，就等于「目标站的锅不算代理头上」，
        这是安全的方向。内置的 `ApiProxyPool` 会连续攒够
        `PROXY_SUSPECT_BAN_AFTER` 次才拉黑。
        """

    def report_good(self, proxy: str) -> None:  # noqa: B027 - 可选钩子
        """这个代理刚成功完成了一次请求 —— 用来把连续失败计数清零。

        ⚠️ 它在**每个成功请求**上都会被调用，实现必须便宜。
        """

    def close(self) -> None:  # noqa: B027 - 可选钩子
        """释放资源。"""
