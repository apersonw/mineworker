"""``Request`` —— 对一次抓取请求的封装。

框架参数（如 ``callback`` ``priority`` ``render``）与 HTTP 参数（``headers``
``params`` ``data`` …）分开存放：HTTP 参数进 :attr:`requests_kwargs`，由下载器
消费；其余未知关键字作为自定义属性存入 :attr:`custom`，可直接 ``request.xxx`` 访问。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mineworker.utils import tools

if TYPE_CHECKING:
    from mineworker.network.downloader.base import Downloader
    from mineworker.network.response import Response

#: 会透传给 HTTP 客户端的关键字
_HTTP_KEYS: frozenset[str] = frozenset(
    {
        "params",
        "headers",
        "cookies",
        "data",
        "json",
        "content",
        "files",
        "auth",
        "timeout",
        "follow_redirects",
        "allow_redirects",
        "verify",
        "proxy",
        "proxies",
        "cert",
    }
)


class Request:
    def __init__(
        self,
        url: str,
        method: str = "GET",
        *,
        callback: str | Callable[..., Any] | None = None,
        priority: int = 300,
        retry_times: int = 0,
        filter_repeat: bool = True,
        auto_request: bool = True,
        use_session: bool | None = None,
        random_user_agent: bool | None = None,
        render: bool = False,
        render_time: float | None = None,
        download_midware: str | None = None,
        parser_name: str | None = None,
        cb_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.url = url
        self.method = method.upper()
        self.callback = callback
        self.priority = priority
        self.retry_times = retry_times
        self.filter_repeat = filter_repeat
        self.auto_request = auto_request
        self.use_session = use_session
        self.random_user_agent = random_user_agent
        self.render = render
        self.render_time = render_time
        self.download_midware = download_midware
        self.parser_name = parser_name
        self.cb_kwargs: dict[str, Any] = cb_kwargs or {}
        self.requests_kwargs: dict[str, Any] = {}
        self.custom: dict[str, Any] = {}
        for key, value in kwargs.items():
            if key in _HTTP_KEYS:
                self.requests_kwargs[key] = value
            else:
                self.custom[key] = value

    # ------------------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        custom = self.__dict__.get("custom")
        if custom is not None and name in custom:
            return custom[name]
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __lt__(self, other: Request) -> bool:
        return self.priority < other.priority

    def __repr__(self) -> str:
        return f"<Request {self.method} {self.url}>"

    # ------------------------------------------------------------------
    @property
    def callback_name(self) -> str | None:
        if self.callback is None:
            return None
        if isinstance(self.callback, str):
            return self.callback
        name = getattr(self.callback, "__name__", None)
        return name if isinstance(name, str) else None

    def _canonical_url(self) -> str:
        parts = urlsplit(self.url)
        query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))

    @property
    def fingerprint(self) -> str:
        body = (
            self.requests_kwargs.get("data")
            or self.requests_kwargs.get("json")
            or self.requests_kwargs.get("params")
            or ""
        )
        return tools.get_fingerprint(self.method, self._canonical_url(), body)

    # ------------------------------------------------------------------
    _SERIALIZABLE: tuple[str, ...] = (
        "priority",
        "retry_times",
        "filter_repeat",
        "auto_request",
        "use_session",
        "random_user_agent",
        "render",
        "render_time",
        "download_midware",
        "parser_name",
        "cb_kwargs",
    )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "url": self.url,
            "method": self.method,
            "callback": self.callback_name,
        }
        for key in self._SERIALIZABLE:
            data[key] = getattr(self, key)
        data["requests_kwargs"] = self.requests_kwargs
        data["custom"] = self.custom
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Request:
        data = dict(data)
        url = data.pop("url")
        method = data.pop("method", "GET")
        requests_kwargs = dict(data.pop("requests_kwargs", {}) or {})
        custom = dict(data.pop("custom", {}) or {})
        known = {key: data[key] for key in ("callback", *cls._SERIALIZABLE) if key in data}
        return cls(url, method, **known, **requests_kwargs, **custom)

    def copy(self) -> Request:
        new = Request.from_dict(self.to_dict())
        if callable(self.callback):
            new.callback = self.callback
        return new

    # ------------------------------------------------------------------
    def download(self, downloader: Downloader | None = None) -> Response:
        """立即下载并返回 :class:`Response`（阶段 02 前用于独立调试）。"""
        from mineworker.network.downloader import download_request

        return download_request(self, downloader)
