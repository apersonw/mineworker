"""识别反爬拦截页。

Cloudflare 之类的挑战页常常返回 **200 + 一段 JS**，框架会当成抓取成功、把这段
毫无内容的壳子当数据入库 —— 静默产出脏数据，比直接报错难排查得多。这里把它识别
出来转成一个可重试的异常，让既有的重试 / 换代理机制接管。

规则刻意保守：**宁可漏报，不可误伤**。只认强特征（专有响应头、专有脚本标记），
不靠「页面里出现某个词」这种容易误杀正常页面的判断。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from mineworker.exceptions import AntiBotError

if TYPE_CHECKING:
    from mineworker.network.response import Response

#: 挑战页通常很小（只有一段 JS）。真实内容页几乎不会这么短。
_TINY_BODY = 2048

_CF_MARKERS = (
    "__cf_chl_",  # 挑战脚本
    "/cdn-cgi/challenge-platform/",
    "cf-browser-verification",
)
_AKAMAI_MARKERS = ("_abck", "bm-verify", "/_sec/cp_challenge/")
_JS_REDIRECT = re.compile(
    rb"(?:window\.)?location(?:\.href)?\s*=|<meta[^>]+http-equiv=['\"]?refresh", re.I
)
_HAS_TEXT = re.compile(rb"<(?:p|h[1-6]|article|table|li)\b", re.I)


def _looks_like_cloudflare(response: Response) -> bool:
    # cf-mitigated 是 Cloudflare 明确标注「这次请求被拦了」的响应头，最可靠
    if response.headers.get("cf-mitigated"):
        return True
    if response.status_code not in (403, 503, 429):
        return False
    body = response.content[:_TINY_BODY].lower()
    return any(m.encode() in body for m in _CF_MARKERS)


def _looks_like_akamai(response: Response) -> bool:
    if response.status_code not in (403, 428):
        return False
    body = response.content[:_TINY_BODY].lower()
    return any(m.encode() in body for m in _AKAMAI_MARKERS)


def _looks_like_js_redirect(response: Response) -> bool:
    """极短、没有任何正文标签、却带跳转脚本的空壳页。"""
    if response.status_code != 200 or len(response.content) > _TINY_BODY:
        return False
    body = response.content
    if _HAS_TEXT.search(body):
        return False
    return bool(_JS_REDIRECT.search(body))


_RULES = (
    ("cloudflare", _looks_like_cloudflare),
    ("akamai", _looks_like_akamai),
    ("js_redirect", _looks_like_js_redirect),
)


def detect(response: Response) -> str | None:
    """返回命中的拦截类型名；没命中返回 ``None``。"""
    for name, rule in _RULES:
        if rule(response):
            return name
    return None


def raise_if_blocked(response: Response) -> None:
    """命中拦截就抛 :class:`~mineworker.exceptions.AntiBotError`。

    该异常继承 ``RequestError``，因此会被 ``ParserControl`` 现有的重试逻辑接管：
    计入失败、重试时 ``pick_proxy`` 自然会从池里换一个代理。
    """
    kind = detect(response)
    if kind is None:
        return
    raise AntiBotError(
        f"疑似被 {kind} 拦截（HTTP {response.status_code}，{len(response.content)} 字节）："
        f"{response.url}"
    )
