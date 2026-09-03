from __future__ import annotations

import mineworker


def test_version_is_nonempty_string() -> None:
    assert isinstance(mineworker.__version__, str)
    assert mineworker.__version__


def test_public_api_present() -> None:
    for name in ("setting", "get_logger", "log", "MineWorkerError"):
        assert hasattr(mineworker, name), name


def test_all_names_resolve() -> None:
    for name in mineworker.__all__:
        assert hasattr(mineworker, name), name
