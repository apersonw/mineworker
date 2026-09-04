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


def resolve_impersonate(request: Request) -> str | None:
    """本次请求要伪装成哪个浏览器：请求级 > 全局设置；空串 / None 表示不伪装。"""
    value = getattr(request, "impersonate", None)
    if value is None:
        value = setting.DOWNLOADER_IMPERSONATE
    return value or None


def send_kwargs(
    request: Request,
    default_timeout: float | None = None,
    *,
    redirect_key: str = "follow_redirects",
) -> dict[str, Any]:
    """把 ``request.requests_kwargs`` 整理成可直接传给 ``client.request`` 的 kwargs。

    ``redirect_key`` 是「跟随重定向」在目标客户端里的参数名：httpx 叫
    ``follow_redirects``，curl_cffi 沿用 requests 的 ``allow_redirects``。
    """
    kwargs = {k: v for k, v in request.requests_kwargs.items() if k not in CLIENT_ONLY_KEYS}
    if "allow_redirects" in kwargs and redirect_key != "allow_redirects":
        kwargs[redirect_key] = kwargs.pop("allow_redirects")
    if "timeout" not in kwargs:
        kwargs["timeout"] = (
            default_timeout if default_timeout is not None else setting.REQUEST_TIMEOUT
        )

    want_ua = request.random_user_agent
    if want_ua is None:
        want_ua = setting.RANDOM_USER_AGENT
    # 伪装浏览器时绝不塞随机 UA：impersonate 会带一整套自洽的浏览器头，
    # 再盖一个来自 UA 池的 UA 就成了「TLS 握手说 Chrome、UA 头说 Firefox」——
    # 这种自相矛盾比不伪装更容易被识别。
    if resolve_impersonate(request):
        want_ua = False
    headers = dict(kwargs.get("headers") or {})
    if want_ua and not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = get_random_user_agent()
    if headers:
        kwargs["headers"] = headers
    return kwargs
