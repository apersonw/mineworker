"""失败请求的两条路：探活 vs 真恢复。

我在代理池 / 账号池 / 中间件重试三处文档里反复写过「落进 `failed_requests.jsonl`，
可以用 `retry --requests` 回放」。那条路径从没验过 ——

实测（站点 503 → 请求失败落盘 → 站点恢复 → `retry --requests`）：
报告「恢复 1，仍失败 0」，**库里 0 行，而文件被删了**。

`retry_requests` 只是重新下载看状态码，不跑回调、不产出 item、不入库。
它自己的 docstring 写得很清楚，是我在文档里把它说成了数据恢复，
而它的日志也帮着误导（「恢复 N」）。这和 v4.15 是同一个形状：
报告成功、删掉最后一份副本、数据仍然缺失。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mineworker import setting
from mineworker.core.base_scheduler import BaseScheduler
from mineworker.network.request import Request


class _Buffer:
    def __init__(self) -> None:
        self.queued: list[Request] = []

    def put(self, request: Request) -> None:
        self.queued.append(request)

    def flush(self) -> None:
        return None


def _scheduler(buffer: _Buffer) -> Any:
    scheduler = BaseScheduler.__new__(BaseScheduler)
    scheduler._request_buffer = buffer
    return scheduler


def _write(path: Path, *urls: str) -> None:
    path.write_text(
        "\n".join(json.dumps(Request(u, callback="parse_page").to_dict()) for u in urls) + "\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setattr(setting, "RETRY_FAILED_ON_START", True)
    monkeypatch.setattr(setting, "FAILED_REQUEST_PATH", str(tmp_path / "failed_requests.jsonl"))
    return tmp_path


def test_failed_requests_are_queued_again(_on: Path) -> None:
    dump = Path(setting.FAILED_REQUEST_PATH)
    _write(dump, "http://example.com/p/1", "http://example.com/p/2")
    buffer = _Buffer()

    assert _scheduler(buffer)._replay_failed_requests() == 2
    assert [r.url for r in buffer.queued] == [
        "http://example.com/p/1",
        "http://example.com/p/2",
    ]
    assert not dump.exists(), "消费完了还留着，下次启动会再灌一遍"


def test_replayed_requests_bypass_dedup(_on: Path) -> None:
    """这些 URL 的指纹已经在去重里了 —— 不清 filter_repeat 就会被静默挡掉，
    变成「回放跑了但什么都没发生」。"""
    _write(Path(setting.FAILED_REQUEST_PATH), "http://example.com/p/1")
    buffer = _Buffer()
    _scheduler(buffer)._replay_failed_requests()
    assert buffer.queued[0].filter_repeat is False


def test_callback_survives_the_round_trip(_on: Path) -> None:
    """回调丢了的话，重放回来的响应不知道该交给谁 —— 会退回默认 parse。"""
    _write(Path(setting.FAILED_REQUEST_PATH), "http://example.com/p/1")
    buffer = _Buffer()
    _scheduler(buffer)._replay_failed_requests()
    assert buffer.queued[0].callback == "parse_page"


def test_switch_off_means_no_replay(monkeypatch: pytest.MonkeyPatch, _on: Path) -> None:
    monkeypatch.setattr(setting, "RETRY_FAILED_ON_START", False)
    dump = Path(setting.FAILED_REQUEST_PATH)
    _write(dump, "http://example.com/p/1")
    buffer = _Buffer()
    assert _scheduler(buffer)._replay_failed_requests() == 0
    assert dump.exists(), "没开开关却把文件消费掉了"


def test_broken_line_does_not_kill_the_whole_replay(_on: Path) -> None:
    """一条坏记录不该连累其余能回放的请求。"""
    dump = Path(setting.FAILED_REQUEST_PATH)
    dump.write_text(
        "{ 不是 json\n" + json.dumps(Request("http://example.com/p/2").to_dict()) + "\n",
        encoding="utf-8",
    )
    buffer = _Buffer()
    assert _scheduler(buffer)._replay_failed_requests() == 1
    assert buffer.queued[0].url == "http://example.com/p/2"


def test_probe_keeps_every_record(monkeypatch: pytest.MonkeyPatch, _on: Path) -> None:
    """探活不是恢复：可达只说明站点回来了，那些页面仍然需要真正重跑。

    早先的版本在这里把成功探到的记录删掉了 —— 那是唯一副本。
    """
    from mineworker.commands import retry as retry_mod
    from mineworker.network.response import Response

    dump = Path(setting.FAILED_REQUEST_PATH)
    _write(dump, "http://example.com/p/1")
    monkeypatch.setattr(
        Request,
        "download",
        lambda self, downloader=None: Response(
            url=self.url, status_code=200, content=b"ok", request=self
        ),
    )
    monkeypatch.setattr(retry_mod, "close_default_downloaders", lambda: None)

    reachable, unreachable = retry_mod.retry_requests(str(dump))
    assert (reachable, unreachable) == (1, 0)
    assert dump.exists(), "探活把失败记录删了 —— 那是唯一副本，而数据并没有回来"
    assert len(dump.read_text(encoding="utf-8").strip().splitlines()) == 1
