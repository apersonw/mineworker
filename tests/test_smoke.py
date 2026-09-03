from __future__ import annotations

import pytest

import mineworker
from mineworker.commands import cmdline


def test_version_is_nonempty_string() -> None:
    assert isinstance(mineworker.__version__, str)
    assert mineworker.__version__


def test_public_api_present() -> None:
    for name in ("setting", "get_logger", "log", "MineWorkerError"):
        assert hasattr(mineworker, name), name


def test_all_names_resolve() -> None:
    for name in mineworker.__all__:
        assert hasattr(mineworker, name), name


def test_cli_version_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        cmdline.main(["--version"])
    assert exc.value.code == 0


def test_cli_pending_subcommand_returns_one() -> None:
    assert cmdline.main(["create"]) == 1


def test_cli_no_args_returns_zero() -> None:
    assert cmdline.main([]) == 0
