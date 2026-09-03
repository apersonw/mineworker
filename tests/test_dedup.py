from __future__ import annotations

import pytest

from mineworker import setting
from mineworker.dedup import Dedup, LiteFilter, MemoryBloomFilter
from mineworker.exceptions import ConfigError


def test_lite_filter_exact() -> None:
    f = LiteFilter()
    assert f.add("a") is True
    assert f.add("a") is False
    assert "a" in f
    assert "b" not in f
    assert len(f) == 1


def test_bloom_filter_basic() -> None:
    f = MemoryBloomFilter(capacity=10_000, error_rate=1e-4)
    assert f.add("x") is True
    assert f.add("x") is False
    assert "x" in f
    assert "y" not in f
    assert len(f) == 1


def test_bloom_no_false_negatives() -> None:
    f = MemoryBloomFilter(capacity=5_000, error_rate=1e-3)
    added = [f"key-{i}" for i in range(2_000)]
    for k in added:
        f.add(k)
    assert all(k in f for k in added)  # 布隆不会漏（无假阴性）


def test_bloom_false_positive_rate_is_low() -> None:
    f = MemoryBloomFilter(capacity=10_000, error_rate=1e-3)
    for i in range(10_000):
        f.add(f"in-{i}")
    fp = sum(f"out-{i}" in f for i in range(10_000))
    assert fp / 10_000 < 0.02  # 宽松上界


def test_bloom_rejects_bad_error_rate() -> None:
    with pytest.raises(ValueError, match="error_rate"):
        MemoryBloomFilter(error_rate=1.5)


def test_dedup_facade_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setting, "DEDUP_FILTER", "memory")
    d = Dedup()
    assert d.add("hello") is True
    assert d.add("hello") is False
    assert d.get("hello") is True
    assert "hello" in d
    assert d.get("nope") is False


def test_dedup_facade_lite_and_dict_values() -> None:
    d = Dedup(filter_type="lite", to_md5=True)
    assert d.add({"b": 2, "a": 1}) is True
    assert d.add({"a": 1, "b": 2}) is False  # 键序无关（sort_keys）


def test_dedup_unknown_filter() -> None:
    with pytest.raises(ConfigError, match="DEDUP_FILTER"):
        Dedup(filter_type="quantum")
