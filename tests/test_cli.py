from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from pytest_httpserver import HTTPServer
from typer.testing import CliRunner

from mineworker import setting
from mineworker.commands import create as gen
from mineworker.commands.cmdline import app
from mineworker.commands.retry import retry_items, retry_requests
from mineworker.pipelines.base import BasePipeline

runner = CliRunner()


class CapturePipeline(BasePipeline):
    rows: list[tuple[str, list[dict[str, Any]]]] = []
    fail: bool = False

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        if CapturePipeline.fail:
            return False
        CapturePipeline.rows.append((table, items))
        return True


_PIPE = f"{__name__}.CapturePipeline"


@pytest.fixture(autouse=True)
def _reset_capture() -> None:
    CapturePipeline.rows.clear()
    CapturePipeline.fail = False


# ---------------------------------------------------------------- name helpers
def test_name_helpers() -> None:
    assert gen._to_camel("news_list") == "NewsList"
    assert gen._to_camel("NewsSpider") == "NewsSpider"
    assert gen._to_camel("my-cool-thing") == "MyCoolThing"
    assert gen._to_snake("NewsSpider") == "news_spider"
    assert gen._to_snake("my-cool-thing") == "my_cool_thing"


# ---------------------------------------------------------------- create
def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "mineworker" in result.stdout


def test_create_without_flags_fails() -> None:
    assert runner.invoke(app, ["create"]).exit_code == 1


def test_create_project_scaffold_compiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["create", "-p", "my_shop"])
    assert result.exit_code == 0

    root = tmp_path / "my_shop"
    for name in ("main.py", "setting.py", "README.md", "spiders/my_shop_spider.py"):
        assert (root / name).exists(), name
    for py in root.rglob("*.py"):
        ast.parse(py.read_text(encoding="utf-8"), str(py))
    assert "MyShopSpider" in (root / "main.py").read_text(encoding="utf-8")


def test_create_project_refuses_nonempty_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "keep").write_text("x")
    assert runner.invoke(app, ["create", "-p", "taken"]).exit_code != 0


def test_create_spider_and_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["create", "-s", "news-list"]).exit_code == 0
    assert runner.invoke(app, ["create", "-i", "NewsDetail"]).exit_code == 0

    spider = (tmp_path / "news_list_spider.py").read_text(encoding="utf-8")
    item = (tmp_path / "news_detail_item.py").read_text(encoding="utf-8")
    assert "class NewsListSpider(mw.AirSpider)" in spider
    assert "class NewsDetailItem(mw.Item)" in item
    assert '__table_name__ = "news_detail"' in item


def test_create_force_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    gen.create_spider("Dupe")
    with pytest.raises(FileExistsError):
        gen.create_spider("Dupe")
    gen.create_spider("Dupe", force=True)  # 不抛


# ---------------------------------------------------------------- shell
def test_shell_namespace(httpserver: HTTPServer) -> None:
    from mineworker.commands.shell import build_namespace

    httpserver.expect_request("/").respond_with_data("<h1>hi</h1>", content_type="text/html")
    ns = build_namespace(httpserver.url_for("/"))
    assert ns["response"].xpath("//h1/text()").get() == "hi"
    assert ns["request"].url == httpserver.url_for("/")
    assert "mw" in ns


# ---------------------------------------------------------------- retry
def test_retry_items_replays_and_clears(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setting, "ITEM_PIPELINES", [_PIPE])
    dump = tmp_path / "failed_items.jsonl"
    dump.write_text(
        '{"table": "news", "data": {"url": "u1"}}\n{"table": "news", "data": {"url": "u2"}}\n',
        encoding="utf-8",
    )

    assert retry_items() == (2, 0)
    assert not dump.exists()
    assert CapturePipeline.rows == [("news", [{"url": "u1"}, {"url": "u2"}])]


def test_retry_items_keeps_still_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setting, "ITEM_PIPELINES", [_PIPE])
    CapturePipeline.fail = True
    dump = tmp_path / "failed_items.jsonl"
    dump.write_text('{"table": "t", "data": {"k": 1}}\n', encoding="utf-8")

    assert retry_items() == (0, 1)
    assert dump.exists()
    assert json.loads(dump.read_text(encoding="utf-8")) == {"table": "t", "data": {"k": 1}}


def test_retry_requests_redownloads(
    httpserver: HTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setting, "RANDOM_USER_AGENT", False)
    httpserver.expect_request("/ok").respond_with_data("ok")
    dump = tmp_path / "failed_requests.jsonl"
    dump.write_text(
        json.dumps({"url": httpserver.url_for("/ok"), "method": "GET"})
        + "\n"
        + json.dumps({"url": "http://127.0.0.1:1/down", "method": "GET"})
        + "\n",
        encoding="utf-8",
    )

    assert retry_requests() == (1, 1)
    assert "down" in dump.read_text(encoding="utf-8")
    assert "/ok" not in dump.read_text(encoding="utf-8")


def test_retry_noop_when_no_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert retry_items() == (0, 0)
    assert retry_requests() == (0, 0)


def test_retry_cli_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setting, "ITEM_PIPELINES", [_PIPE])
    (tmp_path / "failed_items.jsonl").write_text(
        '{"table": "t", "data": {"a": 1}}\n', encoding="utf-8"
    )
    result = runner.invoke(app, ["retry", "--items"])
    assert result.exit_code == 0
    assert "成功 1" in result.stdout
