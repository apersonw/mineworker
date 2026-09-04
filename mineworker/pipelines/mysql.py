"""写入 MySQL。需要 ``pip install mineworker[mysql]``。

``save_items`` 用一条 ``INSERT ... VALUES (...), (...)``（``executemany``）；
``MYSQL_UPDATE_ON_DUPLICATE=True`` 时带 ``ON DUPLICATE KEY UPDATE``，即按唯一键 upsert。
``update_items`` 按 ``__update_key__`` 逐条 UPDATE。

一批数据以第一条的字段为准（和 CsvPipeline 一致）。
"""

from __future__ import annotations

from typing import Any

from mineworker import setting
from mineworker.pipelines.base import BasePipeline
from mineworker.utils.log import get_logger

log = get_logger("pipeline.mysql")


class MysqlPipeline(BasePipeline):
    def __init__(self, *, db: Any = None, **conn: Any) -> None:
        if db is None:
            from mineworker.db.mysqldb import MysqlDB

            db = MysqlDB(**conn)
        self._db = db

    def _insert_sql(self, table: str, cols: list[str]) -> str:
        col_sql = ", ".join(f"`{c}`" for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))
        sql = f"INSERT INTO `{table}` ({col_sql}) VALUES ({placeholders})"
        if setting.MYSQL_UPDATE_ON_DUPLICATE:
            upd = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in cols)
            sql += f" ON DUPLICATE KEY UPDATE {upd}"
        return sql

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        if not items:
            return True
        cols = list(items[0])
        sql = self._insert_sql(table, cols)
        args = [tuple(item.get(c) for c in cols) for item in items]
        try:
            self._db.executemany(sql, args)
        except Exception as exc:
            log.error("[{}] executemany 失败：{!r}", table, exc)
            return False
        return True

    def update_items(self, table: str, items: list[dict[str, Any]], update_keys: list[str]) -> bool:
        if not items:
            return True
        try:
            for item in items:
                set_cols = [c for c in item if c not in update_keys]
                if not set_cols:
                    continue
                set_sql = ", ".join(f"`{c}`=%s" for c in set_cols)
                where_sql = " AND ".join(f"`{k}`=%s" for k in update_keys)
                sql = f"UPDATE `{table}` SET {set_sql} WHERE {where_sql}"
                args = tuple(item[c] for c in set_cols) + tuple(item.get(k) for k in update_keys)
                self._db.execute(sql, args)
        except Exception as exc:
            log.error("[{}] update 失败：{!r}", table, exc)
            return False
        return True

    def close(self) -> None:
        self._db.close()
