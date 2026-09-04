"""发行包正确性：版本一致、类型标记随包、CLI 入口点可解析。

这些断言只有在包被真正安装后才有意义（CI 里 `pip install -e ".[dev]"`）。
本地改完 `__about__.py` 记得重装一次，否则 dist-info 里还是旧版本号。
"""

from __future__ import annotations

import builtins
import importlib.metadata as md
import importlib.resources as res
import sys

import pytest

import mineworker
from mineworker import commands
from mineworker.__about__ import __version__

DIST = "mineworker"


def test_version_matches_installed_metadata() -> None:
    """`__about__.py` 与已安装包的版本号不能漂移。"""
    assert md.version(DIST) == __version__


def test_module_version_forwards_about() -> None:
    assert mineworker.__version__ == __version__


def test_py_typed_ships_with_package() -> None:
    """没有 py.typed，下游装了包也拿不到类型（mypy strict 的成果就白费了）。"""
    assert res.files("mineworker").joinpath("py.typed").is_file()


def test_templates_ship_with_package() -> None:
    """CLI 脚手架依赖包内 jinja 模板，漏打包会让 `mineworker create` 直接崩。"""
    templates = res.files("mineworker.templates")
    for name in ("air_spider.py.jinja", "item.py.jinja", "setting.py.jinja"):
        assert templates.joinpath(name).is_file(), name


def test_console_script_entry_point_resolves() -> None:
    (entry,) = [e for e in md.distribution(DIST).entry_points if e.name == "mineworker"]
    assert entry.group == "console_scripts"
    assert callable(entry.load())


def test_console_script_without_cli_extra_explains_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只装核心包时敲 `mineworker`，要给一句能照做的提示，不是 ModuleNotFoundError 堆栈。"""
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kw: object) -> object:
        if name == "typer" or name.startswith("typer."):
            raise ModuleNotFoundError(f"No module named '{name}'", name="typer")
        return real_import(name, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.delitem(sys.modules, "mineworker.commands.cmdline", raising=False)
    monkeypatch.delitem(sys.modules, "typer", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(SystemExit) as excinfo:
        commands.main()

    assert 'pip install "mineworker[cli]"' in str(excinfo.value)


def test_declared_extras() -> None:
    extras = set(md.metadata(DIST).get_all("Provides-Extra") or [])
    assert {"render", "mongo", "mysql", "redis", "cli", "metrics", "all"} <= extras
