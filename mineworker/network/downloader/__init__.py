"""下载器：HttpxDownloader（阶段 01）；PlaywrightDownloader（阶段 04）。

``download_request`` 是便捷入口：按 Request 选择合适的默认下载器并执行。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from mineworker import setting
from mineworker.network.downloader._common import resolve_impersonate
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
_lock = threading.Lock()


def get_default_downloader(request: Request) -> Downloader:
    if request.render:
        # 渲染必须走浏览器，浏览器自带真实指纹，无需 impersonate
        key = "playwright"
    elif resolve_impersonate(request):
        key = "curl-session" if request.use_session else "curl"
    elif setting.DOWNLOADER_ASYNC:
        key = "async"
    elif request.use_session:
        key = "httpx-session"
    else:
        key = "httpx"
    downloader = _defaults.get(key)
    if downloader is None:
        with _lock:
            downloader = _defaults.get(key)
            if downloader is None:
                downloader = _build(key, request)
                _defaults[key] = downloader
    return downloader


def _build(key: str, request: Request) -> Downloader:
    if key == "playwright":
        from mineworker.network.downloader._playwright import PlaywrightDownloader

        return PlaywrightDownloader()
    if key.startswith("curl"):
        from mineworker.network.downloader._curl import CurlDownloader

        return CurlDownloader(use_session=bool(request.use_session))
    if key == "async":
        from mineworker.network.downloader._async_httpx import AsyncHttpxDownloader

        return AsyncHttpxDownloader()
    return HttpxDownloader(use_session=bool(request.use_session))


def download_request(request: Request, downloader: Downloader | None = None) -> Response:
    return (downloader or get_default_downloader(request)).download(request)


def close_default_downloaders() -> None:
    for downloader in _defaults.values():
        downloader.close()
    _defaults.clear()
