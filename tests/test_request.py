from __future__ import annotations

import pytest

from mineworker import Request


def test_defaults() -> None:
    r = Request("https://example.com/a")
    assert r.method == "GET"
    assert r.priority == 300
    assert r.retry_times == 0
    assert r.filter_repeat is True
    assert r.auto_request is True
    assert r.render is False
    assert r.requests_kwargs == {}
    assert r.custom == {}


def test_method_is_upcased() -> None:
    assert Request("https://e.com", "post").method == "POST"


def test_repr() -> None:
    assert repr(Request("https://e.com/x", "POST")) == "<Request POST https://e.com/x>"


def test_http_kwargs_go_to_requests_kwargs_others_to_custom() -> None:
    r = Request(
        "https://e.com",
        headers={"X": "1"},
        params={"q": "x"},
        batch_id=7,
        note="hi",
    )
    assert r.requests_kwargs == {"headers": {"X": "1"}, "params": {"q": "x"}}
    assert r.custom == {"batch_id": 7, "note": "hi"}
    assert r.batch_id == 7
    assert r.note == "hi"


def test_unknown_attribute_raises() -> None:
    with pytest.raises(AttributeError):
        _ = Request("https://e.com").nope


def test_ordering_by_priority() -> None:
    lo = Request("https://e.com", priority=100)
    hi = Request("https://e.com", priority=500)
    assert lo < hi
    assert sorted([hi, lo])[0] is lo


def test_callback_name() -> None:
    assert Request("https://e.com").callback_name is None
    assert Request("https://e.com", callback="parse_x").callback_name == "parse_x"

    def parse_y(request: object, response: object) -> None: ...

    assert Request("https://e.com", callback=parse_y).callback_name == "parse_y"


def test_fingerprint_is_query_order_independent() -> None:
    a = Request("https://e.com/s?b=2&a=1")
    b = Request("https://e.com/s?a=1&b=2")
    assert a.fingerprint == b.fingerprint


def test_fingerprint_changes_with_method_and_fragment_is_ignored() -> None:
    assert Request("https://e.com/x").fingerprint != Request("https://e.com/x", "POST").fingerprint
    assert Request("https://e.com/x#top").fingerprint == Request("https://e.com/x").fingerprint


def test_fingerprint_uses_body() -> None:
    a = Request("https://e.com/x", "POST", data={"k": 1})
    b = Request("https://e.com/x", "POST", data={"k": 2})
    assert a.fingerprint != b.fingerprint


def test_to_dict_from_dict_roundtrip() -> None:
    r = Request(
        "https://e.com/s?a=1",
        "POST",
        callback="parse_list",
        priority=50,
        render=True,
        cb_kwargs={"page": 2},
        headers={"H": "v"},
        tag="abc",
    )
    clone = Request.from_dict(r.to_dict())
    assert clone.to_dict() == r.to_dict()
    assert clone.url == r.url
    assert clone.method == "POST"
    assert clone.render is True
    assert clone.cb_kwargs == {"page": 2}
    assert clone.requests_kwargs == {"headers": {"H": "v"}}
    assert clone.tag == "abc"


def test_copy_preserves_callable_callback() -> None:
    def parse(request: object, response: object) -> None: ...

    r = Request("https://e.com", callback=parse, headers={"A": "1"})
    clone = r.copy()
    assert clone.callback is parse
    assert clone.requests_kwargs == {"headers": {"A": "1"}}
    assert clone is not r
