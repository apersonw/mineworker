from __future__ import annotations

import datetime as dt
import json

import pytest

from mineworker.utils import tools


def test_md5_is_stable_and_sensitive() -> None:
    assert tools.md5("abc") == tools.md5("abc")
    assert tools.md5("abc") == tools.md5(b"abc")
    assert tools.md5("abc") != tools.md5("abd")


def test_get_fingerprint_is_dict_order_independent() -> None:
    a = tools.get_fingerprint("https://x", {"b": 2, "a": 1})
    b = tools.get_fingerprint("https://x", {"a": 1, "b": 2})
    assert a == b
    assert a != tools.get_fingerprint("https://y", {"a": 1, "b": 2})


def test_dumps_json_keeps_unicode_and_falls_back_to_str() -> None:
    out = tools.dumps_json({"k": "中文", "t": dt.datetime(2026, 1, 1, 12, 0, 0)})
    assert "中文" in out
    assert "2026-01-01" in out
    assert json.loads(out)["k"] == "中文"


def test_json_roundtrip() -> None:
    assert tools.loads_json(tools.dumps_json({"a": [1, 2]})) == {"a": [1, 2]}
    assert tools.loads_json(b'{"x": 1}') == {"x": 1}


def test_load_object_returns_target() -> None:
    assert tools.load_object("json.dumps") is json.dumps


def test_load_object_rejects_bad_paths() -> None:
    with pytest.raises(ValueError, match="对象路径"):
        tools.load_object("nodot")
    with pytest.raises(ModuleNotFoundError):
        tools.load_object("mineworker._no_such_module.x")
    with pytest.raises(ImportError):
        tools.load_object("json.does_not_exist")


def test_retry_succeeds_after_transient_failures() -> None:
    attempts = {"n": 0}

    @tools.retry(times=3, interval=0)
    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("boom")
        return "ok"

    assert flaky() == "ok"
    assert attempts["n"] == 3


def test_retry_reraises_after_exhausting_attempts() -> None:
    attempts = {"n": 0}

    @tools.retry(times=2)
    def always_fail() -> None:
        attempts["n"] += 1
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        always_fail()
    assert attempts["n"] == 3  # 首次 + 2 次重试


def test_retry_only_catches_declared_exceptions() -> None:
    @tools.retry(times=2, exceptions=ValueError)
    def raises_type_error() -> None:
        raise TypeError("unhandled")

    with pytest.raises(TypeError):
        raises_type_error()


def test_time_helpers() -> None:
    assert tools.now().tzinfo is not None
    assert isinstance(tools.current_timestamp(), int)
    assert tools.format_date(dt.datetime(2026, 9, 4, 8, 30, 0)) == "2026-09-04 08:30:00"
