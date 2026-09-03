"""下载器：HttpxDownloader（阶段 01）；PlaywrightDownloader（阶段 04）。

``download_request`` 是便捷入口：按 Request 选择合适的默认下载器并执行。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mineworker.network.downloader._httpx import HttpxDownloader
from mineworker.network.downloader.base import Downloader

if TYPE_CHECKING:
    from mineworker.network.request import Request
    from mineworker.network.response import Response

__all__ = [
    "Downloader",
    "HttpxDownloader",
    "close_default_downloaders",
    "download_request",
    "get_default_downloader",
]

_defaults: dict[str, Downloader] = {}


def get_default_downloader(request: Request) -> Downloader:
    if request.render:
        raise NotImplementedError("浏览器渲染（render=True）在阶段 04 提供，当前仅支持 httpx 下载")
    key = "httpx-session" if request.use_session else "httpx"
    if key not in _defaults:
        _defaults[key] = HttpxDownloader(use_session=bool(request.use_session))
    return _defaults[key]


def download_request(request: Request, downloader: Downloader | None = None) -> Response:
    return (downloader or get_default_downloader(request)).download(request)


def close_default_downloaders() -> None:
    for downloader in _defaults.values():
        downloader.close()
    _defaults.clear()
