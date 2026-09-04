"""命令行（阶段 05）：typer 应用 + create / shell / retry 子命令。

需要 ``pip install mineworker[cli]``（typer + jinja2）。
"""

from __future__ import annotations


def main() -> None:
    """``mineworker`` 控制台脚本入口。

    脚本随核心包一起装，但 CLI 依赖只在 ``[cli]`` extra 里。缺依赖时给一句照着做就能
    修好的提示，而不是甩一个 ``ModuleNotFoundError`` 堆栈给刚 ``pip install`` 完的人。
    """
    try:
        from mineworker.commands.cmdline import main as _main
    except ModuleNotFoundError as exc:  # pragma: no cover - 走到这里说明没装 [cli]
        raise SystemExit(
            f'mineworker 命令行需要额外依赖（缺少 "{exc.name}"）。\n'
            '安装：pip install "mineworker[cli]"'
        ) from exc
    _main()
