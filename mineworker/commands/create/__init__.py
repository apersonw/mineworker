"""脚手架生成：项目 / 爬虫 / Item / setting（`mineworker create`）。"""

from __future__ import annotations

import importlib.resources
import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from mineworker.utils.log import get_logger

log = get_logger("create")

_env = Environment(  # 生成 .py 文件而非 HTML，无需 autoescape
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    autoescape=False,
)
_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _to_snake(name: str) -> str:
    name = re.sub(r"[\s-]+", "_", name.strip())
    return _CAMEL_BOUNDARY.sub("_", name).lower().strip("_")


def _to_camel(name: str) -> str:
    parts = re.split(r"[\s_-]+", name.strip())
    if len(parts) == 1 and not parts[0].islower():
        return parts[0][:1].upper() + parts[0][1:]
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def _render(relpath: str, /, **ctx: Any) -> str:
    source = (
        importlib.resources.files("mineworker.templates")
        .joinpath(relpath)
        .read_text(encoding="utf-8")
    )
    return _env.from_string(source).render(**ctx)


def _write(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} 已存在（加 --force 覆盖）")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("生成 {}", path)


def _spider_names(name: str) -> tuple[str, str]:
    class_name = _to_camel(name)
    if not class_name.endswith("Spider"):
        class_name += "Spider"
    return class_name, _to_snake(class_name)


def _item_names(name: str) -> tuple[str, str, str]:
    class_name = _to_camel(name)
    if not class_name.endswith("Item"):
        class_name += "Item"
    module = _to_snake(class_name)
    table = re.sub(r"_item$", "", module) or module
    return class_name, module, table


# ----------------------------------------------------------------------
def create_spider(name: str, *, target_dir: Path | None = None, force: bool = False) -> Path:
    class_name, module = _spider_names(name)
    _, _, table = _item_names(name)
    path = (target_dir or Path()) / f"{module}.py"
    _write(
        path,
        _render("air_spider.py.jinja", class_name=class_name, table_name=table),
        force=force,
    )
    return path


def create_item(name: str, *, target_dir: Path | None = None, force: bool = False) -> Path:
    class_name, module, table = _item_names(name)
    path = (target_dir or Path()) / f"{module}.py"
    _write(
        path,
        _render("item.py.jinja", class_name=class_name, table_name=table),
        force=force,
    )
    return path


def create_setting(*, target_dir: Path | None = None, force: bool = False) -> Path:
    path = (target_dir or Path()) / "setting.py"
    _write(path, _render("setting.py.jinja", project_name="mineworker"), force=force)
    return path


def create_project(name: str, *, force: bool = False) -> Path:
    root = Path(name)
    if root.exists() and any(root.iterdir()) and not force:
        raise FileExistsError(f"{root} 已存在且非空（加 --force 覆盖）")

    project = _to_snake(name)
    spider_class, spider_module = _spider_names(name)

    _write(
        root / "main.py",
        _render(
            "project/main.py.jinja",
            project_name=project,
            spider_module=spider_module,
            spider_class=spider_class,
        ),
        force=True,
    )
    _write(
        root / "setting.py",
        _render("setting.py.jinja", project_name=project),
        force=True,
    )
    _write(
        root / "README.md",
        _render(
            "project/README.md.jinja",
            project_name=project,
            spider_module=spider_module,
        ),
        force=True,
    )
    _write(root / "spiders" / "__init__.py", "", force=True)
    _write(
        root / "spiders" / f"{spider_module}.py",
        _render(
            "project/spider.py.jinja",
            project_name=project,
            spider_class=spider_class,
        ),
        force=True,
    )
    _write(root / "items" / "__init__.py", "", force=True)
    return root
