from __future__ import annotations

import netspy


def test_version_is_nonempty_string() -> None:
    assert isinstance(netspy.__version__, str)
    assert netspy.__version__


def test_public_api_present() -> None:
    for name in ("setting", "get_logger", "log", "NetspyError"):
        assert hasattr(netspy, name), name


def test_all_names_resolve() -> None:
    for name in netspy.__all__:
        assert hasattr(netspy, name), name
