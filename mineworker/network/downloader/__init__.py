"""下载器：HttpxDownloader（阶段 01）；PlaywrightDownloader（阶段 04）。

``download_request`` 是便捷入口：按 Request 选择合适的默认下载器并执行。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from mineworker import setting
from mineworker.network import throttle
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


def _wants_session(request: Request) -> bool:
    """请求级 > 全局 ``setting.USE_SESSION``。

    `Request.use_session` 默认 None，此前只看它、完全没读 setting，
    于是 `USE_SESSION` 成了一个「定义了、文档写了、但永远不生效」的死配置。
    """
    if request.use_session is not None:
        return bool(request.use_session)
    return bool(setting.USE_SESSION)


def get_default_downloader(request: Request) -> Downloader:
    want_session = _wants_session(request)
    if request.render:
        # 渲染必须走浏览器，浏览器自带真实指纹，无需 impersonate
        key = "playwright"
    elif resolve_impersonate(request):
        key = "curl-session" if want_session else "curl"
    elif setting.DOWNLOADER_ASYNC:
        key = "async"
    elif want_session:
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

        return CurlDownloader(use_session=_wants_session(request))
    if key == "async":
        from mineworker.network.downloader._async_httpx import AsyncHttpxDownloader

        return AsyncHttpxDownloader()
    return HttpxDownloader(use_session=_wants_session(request))


def download_request(request: Request, downloader: Downloader | None = None) -> Response:
    # 限速放在这里而不是中间件里：parser_control 中 process_request 与下载处在两个
    # 独立的 try，下载抛异常时 process_response 不会执行，中间件拿的名额会泄漏。
    # 这里的 with 保证无论成功失败都释放。
    with throttle.slot(request.url):
        response = (downloader or get_default_downloader(request)).download(request)
    if setting.ANTIBOT_DETECT:
        # 放在这里而不是各下载器里：httpx / curl / async / playwright 一次覆盖
        from mineworker.network import antibot

        antibot.raise_if_blocked(response)
    return response


def close_default_downloaders() -> None:
    for downloader in _defaults.values():
        downloader.close()
    _defaults.clear()
