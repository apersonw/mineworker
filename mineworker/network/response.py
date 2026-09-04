"""``Response`` —— 对下载结果的封装，提供文本解码与 parsel 选择器。"""

from __future__ import annotations

import base64
import json as _json
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from parsel import Selector, SelectorList
from w3lib.encoding import html_to_unicode

if TYPE_CHECKING:
    import httpx

    from mineworker.network.request import Request


class Response:
    def __init__(
        self,
        *,
        url: str,
        status_code: int,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        request: Request | None = None,
        encoding: str | None = None,
        elapsed: float | None = None,
        history: list[str] | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.content = content
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.cookies = cookies or {}
        self.request = request
        self.elapsed = elapsed
        self.history = history or []
        self._forced_encoding = encoding
        self._encoding: str | None = None
        self._text: str | None = None
        self._selector: Selector | None = None

    # ------------------------------------------------------------------
    @classmethod
    def from_httpx(cls, resp: httpx.Response, request: Request | None = None) -> Response:
        try:
            elapsed: float | None = resp.elapsed.total_seconds()
        except RuntimeError:
            elapsed = None
        return cls(
            url=str(resp.url),
            status_code=resp.status_code,
            content=resp.content,
            headers=dict(resp.headers),
            cookies=dict(resp.cookies),
            request=request,
            encoding=resp.charset_encoding,
            elapsed=elapsed,
            history=[str(r.url) for r in resp.history],
        )

    @classmethod
    def from_curl_cffi(cls, resp: Any, request: Request | None = None) -> Response:
        """由 :mod:`curl_cffi` 的响应构造（与 :meth:`from_httpx` 平行）。

        与 httpx 的差异：``elapsed`` 已经是秒（float）而非 timedelta，
        编码属性叫 ``charset_encoding``，``history`` 里是完整响应对象。
        """
        elapsed = resp.elapsed
        if hasattr(elapsed, "total_seconds"):
            elapsed = elapsed.total_seconds()
        return cls(
            url=str(resp.url),
            status_code=resp.status_code,
            content=resp.content or b"",
            headers=dict(resp.headers),
            cookies=dict(resp.cookies),
            request=request,
            encoding=getattr(resp, "charset_encoding", None),
            elapsed=float(elapsed) if elapsed is not None else None,
            history=[str(r.url) for r in (resp.history or [])],
        )

    # ------------------------------------------------------------------
    def _decode(self) -> None:
        if self._forced_encoding:
            self._encoding = self._forced_encoding
            self._text = self.content.decode(self._forced_encoding, errors="replace")
            return
        content_type = self.headers.get("content-type", "")
        enc, text = html_to_unicode(content_type, self.content, default_encoding="utf-8")
        self._encoding = enc
        self._text = text

    @property
    def encoding(self) -> str:
        if self._encoding is None:
            self._decode()
        assert self._encoding is not None
        return self._encoding

    @property
    def text(self) -> str:
        if self._text is None:
            self._decode()
        assert self._text is not None
        return self._text

    def json(self, **kwargs: Any) -> Any:
        return _json.loads(self.text, **kwargs)

    # ------------------------------------------------------------------
    @property
    def selector(self) -> Selector:
        if self._selector is None:
            self._selector = Selector(text=self.text)
        return self._selector

    def xpath(self, query: str) -> SelectorList[Selector]:
        return self.selector.xpath(query)

    def css(self, query: str) -> SelectorList[Selector]:
        return self.selector.css(query)

    def re(self, regex: str, replace_entities: bool = True) -> list[str]:
        return self.selector.re(regex, replace_entities=replace_entities)

    def re_first(
        self, regex: str, default: str | None = None, replace_entities: bool = True
    ) -> str | None:
        return self.selector.re_first(regex, default=default, replace_entities=replace_entities)

    @property
    def bs4(self) -> Any:
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:  # pragma: no cover - 可选依赖
            raise ImportError("使用 .bs4 需先安装 beautifulsoup4") from exc
        return BeautifulSoup(self.text, "html.parser")

    # ------------------------------------------------------------------
    def urljoin(self, url: str) -> str:
        return urljoin(self.url, url)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def __repr__(self) -> str:
        return f"<Response [{self.status_code}] {self.url}>"

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status_code": self.status_code,
            "headers": self.headers,
            "cookies": self.cookies,
            "encoding": self._forced_encoding,
            "elapsed": self.elapsed,
            "history": self.history,
            "content_b64": base64.b64encode(self.content).decode("ascii"),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Response:
        return cls(
            url=data["url"],
            status_code=data["status_code"],
            content=base64.b64decode(data.get("content_b64", "")),
            headers=data.get("headers"),
            cookies=data.get("cookies"),
            encoding=data.get("encoding"),
            elapsed=data.get("elapsed"),
            history=data.get("history"),
        )
