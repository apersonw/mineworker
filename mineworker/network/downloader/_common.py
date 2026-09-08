"""httpx 同步 / 异步下载器共用的纯函数。"""

from __future__ import annotations

import asyncio
import ssl
import threading
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import httpx

from mineworker import setting
from mineworker.exceptions import (
    ContentTypeRejectedError,
    ProxyUnavailableError,
    ResponseTooLargeError,
)
from mineworker.network.proxy_pool import get_proxy_pool
from mineworker.network.user_agent import get_random_user_agent

if TYPE_CHECKING:
    from mineworker.network.request import Request

# httpx 0.28 用 follow_redirects 取代 allow_redirects；这些键只能给 client，不能随请求传
CLIENT_ONLY_KEYS = frozenset({"verify", "proxy", "proxies", "cookies"})

# ----------------------------------------------------------------------
# SSL context 缓存
#
# httpx.Client() 每次构造都会新建一个 SSLContext（加载 CA 包）——实测 ~33ms/个。
# 而下载器默认每个请求新建一个 Client（use_session=False），于是这 33ms 变成了
# 每请求的固定开销，是框架里最大的单项成本。把 context 缓存下来复用后降到 ~0.4ms。
#
# 复用 SSLContext 是安全的：它是无状态的配置对象，httpx / ssl 模块本身也鼓励共享。
# 注意这与「共享 Client」不同 —— 后者会连 cookie jar 一起共享，改变抓取语义。
_ssl_cache: dict[Any, ssl.SSLContext] = {}
_ssl_lock = threading.Lock()


def ssl_context_for(verify: Any) -> Any:
    """把 ``verify`` 值换成可复用的 ``SSLContext``；不可缓存的原样返回。"""
    # False（不校验）和已经是 SSLContext 的，都没有构造开销
    if verify is False or isinstance(verify, ssl.SSLContext):
        return verify
    key = verify if isinstance(verify, str) else True
    cached = _ssl_cache.get(key)
    if cached is not None:
        return cached
    with _ssl_lock:
        cached = _ssl_cache.get(key)
        if cached is None:
            cached = httpx.create_ssl_context(verify=verify)
            _ssl_cache[key] = cached
    return cached


def _attempt_proxy(request: Request, fallback: str | None) -> tuple[str | None, bool]:
    """一次尝试。返回 (代理, 是否该再等等)。

    判定逻辑只写这一份 —— 同步和异步两条路径只有「怎么等」不同，
    复制两份判定迟早会分叉。
    """
    rk = request.requests_kwargs
    explicit = rk.get("proxy") or rk.get("proxies") or fallback
    if explicit:
        return explicit, False
    pool = get_proxy_pool()
    if pool is None:
        return None, False  # 压根没开代理池，直连本来就是行为
    proxy = pool.get_proxy()
    if proxy or setting.PROXY_ALLOW_DIRECT:
        return proxy, False
    return None, True


def _no_proxy_error(request: Request) -> ProxyUnavailableError:
    return ProxyUnavailableError(
        f"代理池取不到代理（等了 {setting.PROXY_WAIT_TIMEOUT} 秒），"
        f"不回退直连：{request.method} {request.url}。"
        "确实想要「有代理就用、没有就直连」的话，把 PROXY_ALLOW_DIRECT 打开"
    )


def _proxy_gap() -> float:
    """两次重取之间歇多久 —— 比取号接口的最小间隔更密没有意义。"""
    return max(setting.PROXY_MIN_INTERVAL, 0.1)


def pick_proxy(request: Request, fallback: str | None = None) -> str | None:
    """请求显式指定 > 下载器固定代理 > 代理池。

    开着代理池却取不到代理时**不回退直连** —— 那会把源 IP 暴露给目标站，
    而开代理池的全部意义就是别这么干。先有界等待并重取
    （供应商短暂断供不至于把任务的重试次数耗光），到点仍拿不到才抛。
    """
    deadline: float | None = None
    while True:
        proxy, wait = _attempt_proxy(request, fallback)
        if not wait:
            return proxy
        if deadline is None:
            deadline = time.monotonic() + setting.PROXY_WAIT_TIMEOUT
        if time.monotonic() >= deadline:
            raise _no_proxy_error(request)
        # 等在池的锁**之外**：睡在锁里的话所有线程会一起卡在锁上
        time.sleep(_proxy_gap())


async def apick_proxy(request: Request, fallback: str | None = None) -> str | None:
    """`pick_proxy` 的异步版。

    异步下载器里不能用阻塞式 sleep —— 那会把整个事件循环卡住，
    其它并发请求跟着一起停。
    """
    deadline: float | None = None
    while True:
        proxy, wait = _attempt_proxy(request, fallback)
        if not wait:
            return proxy
        if deadline is None:
            deadline = time.monotonic() + setting.PROXY_WAIT_TIMEOUT
        if time.monotonic() >= deadline:
            raise _no_proxy_error(request)
        await asyncio.sleep(_proxy_gap())


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


# ----------------------------------------------------------------------
# 资源边界：响应体大小上限 + Content-Type 白名单
#
# 三个下载器（httpx / async httpx / curl_cffi）共用这两个函数。它们都工作在
# **拿到响应头、还没读 body** 的时刻 —— 这正是能省下带宽的唯一窗口。
def check_content_type(headers: Any, url: str) -> None:
    """Content-Type 不在白名单就抛 `ContentTypeRejectedError`（调用方据此断开连接）。"""
    allowed = setting.ALLOWED_CONTENT_TYPES
    if not allowed:
        return
    raw = headers.get("content-type") or headers.get("Content-Type") or ""
    ctype = raw.split(";")[0].strip().lower()
    if not ctype:
        # 不少站点根本不发 Content-Type。没有信息时不替它下判断 ——
        # 白名单是用来挡掉「明确说了自己是视频」的响应，不是用来挡沉默的
        return
    if not any(ctype.startswith(prefix.lower()) for prefix in allowed):
        raise ContentTypeRejectedError(f"Content-Type {ctype!r} 不在白名单：{url}")


def check_size(body: bytes, url: str) -> None:
    """对**已经拿到**的完整 body 判上限（渲染这条路用）。

    渲染没有可以边读边停的字节流 —— 内容是从浏览器里取的，只能事后判。
    但仍然要判：否则 ``render=True`` 就成了 `MAX_RESPONSE_SIZE` 的一个后门。
    """
    limit = setting.MAX_RESPONSE_SIZE
    if limit > 0 and len(body) > limit:
        raise ResponseTooLargeError(
            f"响应体 {len(body)} 字节，超过 MAX_RESPONSE_SIZE={limit}：{url}"
        )


def read_capped(chunks: Iterable[bytes], url: str, declared: str | None = None) -> bytes:
    """按 `MAX_RESPONSE_SIZE` 边读边计数，超限立刻抛错（调用方退出上下文即断开）。

    ``declared`` 是 ``Content-Length`` 头，只用来**提前**失败，绝不用来放行：
    它报的是**压缩后**的大小，一个 200KB 的 gzip 可以解压成 200MB。真正的判据是
    下面循环里累计的**解压后**字节数。

    .. note::
       对高压缩比的响应，这个上限**限制的是「留下多少」而不是「峰值分配多少」**：
       httpx / curl 会把每个网络分片整个解压，压缩包一次到齐时解压器就会一次性
       吐出全部内容，我们只能在那之后判定超限。实测 200KB→200MB 的 gzip 炸弹：
       进程仍瞬时涨 ~180MB（不设上限时是 618MB，因为 ``.text`` 还要再解码一份）。
       换句话说：**上限让请求快速失败并挡住二次放大，但挡不住那一次解压分配。**
    """
    limit = setting.MAX_RESPONSE_SIZE
    if limit <= 0:
        return b"".join(chunks)
    if declared:
        try:
            if int(declared) > limit:
                raise ResponseTooLargeError(
                    f"响应体声明 {int(declared)} 字节，超过 MAX_RESPONSE_SIZE={limit}：{url}"
                )
        except ValueError:  # 头是坏的，交给下面按实际字节数判
            pass
    buf = bytearray()
    for chunk in chunks:
        buf += chunk
        if len(buf) > limit:
            raise ResponseTooLargeError(
                f"响应体超过 MAX_RESPONSE_SIZE={limit} 字节，已中止传输：{url}"
            )
    return bytes(buf)
