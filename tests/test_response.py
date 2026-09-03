from __future__ import annotations

from mineworker import Response

HTML = """<!doctype html><html><head><title>T</title></head>
<body><h1 class="x">Hello</h1><a href="/next">n</a><p>id=42</p></body></html>"""


def make(
    content: bytes,
    headers: dict[str, str] | None = None,
    encoding: str | None = None,
) -> Response:
    return Response(
        url="https://example.com/p/",
        status_code=200,
        content=content,
        headers=headers or {"content-type": "text/html; charset=utf-8"},
        encoding=encoding,
    )


def test_text_utf8() -> None:
    r = make("中文标题".encode(), {"content-type": "text/html; charset=utf-8"})
    assert r.text == "中文标题"
    assert r.encoding.lower() in {"utf-8", "utf8", "cp65001"}


def test_text_detects_meta_charset_gbk() -> None:
    body = "<html><head><meta charset='gbk'></head><body>你好</body></html>".encode("gbk")
    r = make(body, {"content-type": "text/html"})
    assert "你好" in r.text
    assert r.encoding.lower().replace("-", "") in {"gbk", "gb2312", "gb18030"}


def test_forced_encoding_wins() -> None:
    r = make("你好".encode("gbk"), {"content-type": "text/html"}, encoding="gbk")
    assert r.text == "你好"
    assert r.encoding == "gbk"


def test_selectors() -> None:
    r = make(HTML.encode())
    assert r.xpath('//h1[@class="x"]/text()').get() == "Hello"
    assert r.css("h1.x::text").get() == "Hello"
    assert r.re(r"id=(\d+)") == ["42"]
    assert r.re_first(r"id=(\d+)") == "42"
    assert r.re_first(r"nomatch(\d+)", default="-") == "-"


def test_json() -> None:
    r = make(b'{"a": [1, 2], "b": "x"}', {"content-type": "application/json"})
    assert r.json() == {"a": [1, 2], "b": "x"}


def test_urljoin_and_ok_and_repr() -> None:
    r = make(HTML.encode())
    assert r.urljoin("/next") == "https://example.com/next"
    assert r.urljoin(r.xpath("//a/@href").get() or "") == "https://example.com/next"
    assert r.ok is True
    assert repr(r) == "<Response [200] https://example.com/p/>"


def test_not_ok() -> None:
    r = Response(url="https://e.com", status_code=404)
    assert r.ok is False


def test_to_dict_from_dict_roundtrip() -> None:
    r = make("中文".encode("gbk"), {"content-type": "text/html"}, encoding="gbk")
    clone = Response.from_dict(r.to_dict())
    assert clone.url == r.url
    assert clone.status_code == 200
    assert clone.content == r.content
    assert clone.text == "中文"
