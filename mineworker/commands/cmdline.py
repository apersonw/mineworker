"""``mineworker`` 命令行入口（阶段 05 用 typer 重写，当前为占位实现）。"""

from __future__ import annotations

import argparse
import sys

from mineworker.__about__ import __version__

_PENDING = {
    "create": "阶段 05：生成项目 / 爬虫 / Item / setting",
    "shell": "阶段 05：交互式抓取调试",
    "retry": "阶段 06：回放失败的请求 / Item",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mineworker", description="MineWorker 爬虫框架命令行")
    parser.add_argument("-V", "--version", action="version", version=f"mineworker {__version__}")
    sub = parser.add_subparsers(dest="command")
    for name, help_text in _PENDING.items():
        sub.add_parser(name, help=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in _PENDING:
        print(f"`mineworker {args.command}` 尚未实现：{_PENDING[args.command]}")
        return 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
