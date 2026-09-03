"""下载器抽象基类。"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import TracebackType

    from mineworker.network.request import Request
    from mineworker.network.response import Response


class Downloader(abc.ABC):
    """把一个 ``Request`` 变成 ``Response``。"""

    @abc.abstractmethod
    def download(self, request: Request) -> Response: ...

    def close(self) -> None:  # noqa: B027 - 可选生命周期钩子，子类按需覆盖
        """释放连接池等资源。默认无操作。"""

    def __enter__(self) -> Downloader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
