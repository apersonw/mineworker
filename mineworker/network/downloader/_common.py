"""httpx 同步 / 异步下载器共用的纯函数。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mineworker import setting
from mineworker.network.proxy_pool import get_proxy_pool
from mineworker.network.user_agent import get_random_user_agent

if TYPE_CHECKING:
    from mineworker.network.request import Request

# httpx 0.28 用 follow_redirects 取代 allow_redirects；这些键只能给 client，不能随请求传
CLIENT_ONLY_KEYS = frozenset({"verify", "proxy", "proxies", "cookies"})


def pick_proxy(request: Request, fallback: str | None = None) -> str | None:
    """请求显式指定 > 下载器固定代理 > 代理池。"""
    rk = request.requests_kwargs
    explicit = rk.get("proxy") or rk.get("proxies") or fallback
    if explicit:
        return explicit
    pool = get_proxy_pool()
    return pool.get_proxy() if pool is not None else None


def report_bad_proxy(proxy: str) -> None:
    """下载失败时把代理反馈给代理池（池未启用则无操作）。"""
    pool = get_proxy_pool()
    if pool is not None:
        pool.report_bad(proxy)


def send_kwargs(request: Request, default_timeout: float | None = None) -> dict[str, Any]:
    """把 ``request.requests_kwargs`` 整理成可直接传给 ``client.request`` 的 kwargs。"""
    kwargs = {k: v for k, v in request.requests_kwargs.items() if k not in CLIENT_ONLY_KEYS}
    if "allow_redirects" in kwargs:
        kwargs["follow_redirects"] = kwargs.pop("allow_redirects")
    if "timeout" not in kwargs:
        kwargs["timeout"] = (
            default_timeout if default_timeout is not None else setting.REQUEST_TIMEOUT
        )

    want_ua = request.random_user_agent
    if want_ua is None:
        want_ua = setting.RANDOM_USER_AGENT
    headers = dict(kwargs.get("headers") or {})
    if want_ua and not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = get_random_user_agent()
    if headers:
        kwargs["headers"] = headers
    return kwargs
