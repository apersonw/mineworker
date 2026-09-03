"""`mineworker retry` —— 回放 dump 文件里的失败请求 / 数据。

- ``--items``：把 ``failed_items.jsonl`` 里的记录重新过一遍当前 ``ITEM_PIPELINES``
- ``--requests``：把 ``failed_requests.jsonl`` 里的请求重新下载一遍（不重跑 parse），
  用于确认目标是否恢复

两者都会把仍失败的记录写回文件，全部成功则删除文件。
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from mineworker import setting
from mineworker.exceptions import RequestError
from mineworker.network.downloader import close_default_downloaders
from mineworker.network.request import Request
from mineworker.utils import tools
from mineworker.utils.log import get_logger

log = get_logger("retry")


def _read_lines(path: Path) -> list[Any]:
    if not path.is_file():
        return []
    return [
        tools.loads_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _rewrite(path: Path, remaining: list[Any]) -> None:
    if remaining:
        path.write_text("\n".join(tools.dumps_json(r) for r in remaining) + "\n", encoding="utf-8")
    elif path.exists():
        path.unlink()


def retry_items(path: str | None = None) -> tuple[int, int]:
    """返回 (成功条数, 仍失败条数)。"""
    file = Path(path or setting.FAILED_ITEM_PATH)
    records = _read_lines(file)
    if not records:
        return (0, 0)

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[record["table"]].append(record["data"])

    pipelines = [tools.load_object(p)() for p in setting.ITEM_PIPELINES]
    ok = 0
    remaining: list[Any] = []
    try:
        for table, datas in groups.items():
            if all(p.save_items(table, datas) for p in pipelines):
                ok += len(datas)
            else:
                remaining.extend({"table": table, "data": d} for d in datas)
    finally:
        for p in pipelines:
            p.close()

    _rewrite(file, remaining)
    log.info("failed_items 回放：成功 {}，仍失败 {}", ok, len(remaining))
    return (ok, len(remaining))


def retry_requests(path: str | None = None) -> tuple[int, int]:
    """重新下载失败请求。返回 (恢复条数, 仍失败条数)。"""
    file = Path(path or setting.FAILED_REQUEST_PATH)
    records = _read_lines(file)
    if not records:
        return (0, 0)

    ok = 0
    remaining: list[Any] = []
    try:
        for record in records:
            request = Request.from_dict(record)
            request.filter_repeat = False
            try:
                response = request.download()
            except RequestError:
                remaining.append(record)
                continue
            if response.ok:
                ok += 1
            else:
                remaining.append(record)
    finally:
        close_default_downloaders()

    _rewrite(file, remaining)
    log.info("failed_requests 回放：恢复 {}，仍失败 {}", ok, len(remaining))
    return (ok, len(remaining))
