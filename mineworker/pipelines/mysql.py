"""写入 MySQL。需要 ``pip install mineworker[mysql]``。

``save_items`` 用一条 ``INSERT ... VALUES (...), (...)``（``executemany``）；
``MYSQL_UPDATE_ON_DUPLICATE=True`` 时带 ``ON DUPLICATE KEY UPDATE``，即按唯一键 upsert。
``update_items`` 按 ``__update_key__`` 逐条 UPDATE。

一批数据以第一条的字段为准（和 CsvPipeline 一致）。骨架见 :mod:`~mineworker.pipelines._sql`。
"""

from __future__ import annotations

from typing import Any

from mineworker import setting
from mineworker.pipelines._sql import SqlPipeline


class MysqlPipeline(SqlPipeline):
    log_name = "pipeline.mysql"

    def __init__(self, *, db: Any = None, **conn: Any) -> None:
        if db is None:
            from mineworker.db.mysqldb import MysqlDB

            db = MysqlDB(**conn)
        super().__init__(db)

    def _quote(self, ident: str) -> str:
        return f"`{ident}`"

    def _conflict_clause(self, cols: list[str]) -> str:
        if not setting.MYSQL_UPDATE_ON_DUPLICATE:
            return ""
        upd = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in cols)
        return f" ON DUPLICATE KEY UPDATE {upd}"
