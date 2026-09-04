"""``mineworker`` 命令行入口（typer）。"""

from __future__ import annotations

from typing import Annotated

import typer

from mineworker.__about__ import __version__

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="MineWorker 爬虫框架命令行",
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"mineworker {__version__}")
        raise typer.Exit


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option("-V", "--version", callback=_version, is_eager=True, help="版本号"),
    ] = False,
) -> None:
    pass


@app.command()
def create(
    project: Annotated[str | None, typer.Option("-p", "--project", help="生成项目脚手架")] = None,
    spider: Annotated[str | None, typer.Option("-s", "--spider", help="生成一个 AirSpider")] = None,
    item: Annotated[str | None, typer.Option("-i", "--item", help="生成一个 Item")] = None,
    table: Annotated[
        str | None,
        typer.Option("--table", help="配合 -i：读该 MySQL 表结构反射字段"),
    ] = None,
    mysql: Annotated[
        str | None,
        typer.Option("--mysql", help="MySQL 连接串 mysql://user:pass@host/db（默认取 setting）"),
    ] = None,
    setting_file: Annotated[bool, typer.Option("--setting", help="生成 setting.py")] = False,
    force: Annotated[bool, typer.Option("-f", "--force", help="覆盖已存在文件")] = False,
) -> None:
    """生成项目 / 爬虫 / Item / 配置文件。"""
    from mineworker.commands import create as gen

    if project:
        root = gen.create_project(project, force=force)
        typer.echo(f"✓ 项目已生成 → cd {root} && python main.py")
    elif spider:
        typer.echo(f"✓ {gen.create_spider(spider, force=force)}")
    elif item:
        typer.echo(f"✓ {gen.create_item(item, force=force, table=table, mysql=mysql)}")
    elif setting_file:
        typer.echo(f"✓ {gen.create_setting(force=force)}")
    else:
        typer.echo("指定 -p / -s / -i / --setting 之一", err=True)
        raise typer.Exit(1)


@app.command()
def shell(
    url: Annotated[str, typer.Argument(help="要抓取的 URL")],
    render: Annotated[bool, typer.Option("--render", help="用浏览器渲染")] = False,
) -> None:
    """抓一个页面并进入交互式 shell（变量 request / response）。"""
    from mineworker.commands.shell import run_shell

    run_shell(url, render=render)


@app.command()
def retry(
    requests: Annotated[
        bool, typer.Option("--requests", help="回放 failed_requests.jsonl")
    ] = False,
    items: Annotated[bool, typer.Option("--items", help="回放 failed_items.jsonl")] = False,
) -> None:
    """回放 dump 文件里的失败请求 / 数据（默认两者都回放）。"""
    from mineworker.commands.retry import retry_items, retry_requests

    if not requests and not items:
        requests = items = True
    if items:
        ok, failed = retry_items()
        typer.echo(f"failed_items：成功 {ok}，仍失败 {failed}")
    if requests:
        ok, failed = retry_requests()
        typer.echo(f"failed_requests：恢复 {ok}，仍失败 {failed}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
