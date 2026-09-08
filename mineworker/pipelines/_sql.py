"""SQL 系管道的公共骨架。

``MysqlPipeline`` / ``PostgresPipeline`` 的写入逻辑八成是重合的：一批 dict 取第一条
的字段当列名，拼成一条 ``INSERT`` 走 ``executemany``；``UpdateItem`` 则按 update_keys
逐条 ``UPDATE``。方言差异只有两处 —— 标识符怎么引用、upsert 子句怎么写 —— 由子类填。
"""

from __future__ import annotations

import abc
from typing import Any

from mineworker.pipelines.base import BasePipeline
from mineworker.utils.log import get_logger


class SqlPipeline(BasePipeline):
    #: 日志名，子类覆盖成 ``pipeline.mysql`` 之类
    log_name = "pipeline.sql"

    def __init__(self, db: Any) -> None:
        self._db = db
        self._log = get_logger(self.log_name)

    # ---- 方言 ---------------------------------------------------------
    @abc.abstractmethod
    def _quote(self, ident: str) -> str:
        """引用一个标识符（表名 / 列名）。"""

    def _conflict_clause(self, cols: list[str]) -> str:
        """``INSERT`` 末尾的冲突处理子句，默认没有（冲突即报错）。"""
        return ""

    # ---- 共用 ---------------------------------------------------------
    def _insert_sql(self, table: str, cols: list[str]) -> str:
        col_sql = ", ".join(self._quote(c) for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))
        sql = f"INSERT INTO {self._quote(table)} ({col_sql}) VALUES ({placeholders})"
        return sql + self._conflict_clause(cols)

    def save_items(self, table: str, items: list[dict[str, Any]]) -> bool:
        if not items:
            return True
        cols = list(items[0])
        sql = self._insert_sql(table, cols)
        args = [tuple(item.get(c) for c in cols) for item in items]
        try:
            self._db.executemany(sql, args)
        except Exception as exc:
            self._log.error("[{}] executemany 失败：{!r}", table, exc)
            return False
        return True

    def update_items(self, table: str, items: list[dict[str, Any]], update_keys: list[str]) -> bool:
        """按 ``update_keys`` 逐条 ``UPDATE``。**一条都没匹配到就算失败。**

        裸 UPDATE 匹配不到行时不报错、只影响 0 行。忽略这个数字的话，
        指向不存在记录的 `UpdateItem` 会被当成写入成功：数据没进库、不会 dump、
        去重指纹照记 —— 那条 URL 从此不会再被抓。

        想要「不存在就插入」，用 ``MYSQL_UPDATE_ON_DUPLICATE``
        或 ``POSTGRES_ON_CONFLICT="update"`` 走 upsert，而不是指望这里。
        """
        if not items:
            return True
        missed: list[tuple[Any, ...]] = []
        try:
            for item in items:
                set_cols = [c for c in item if c not in update_keys]
                if not set_cols:
                    continue
                set_sql = ", ".join(f"{self._quote(c)}=%s" for c in set_cols)
                where_sql = " AND ".join(f"{self._quote(k)}=%s" for k in update_keys)
                sql = f"UPDATE {self._quote(table)} SET {set_sql} WHERE {where_sql}"
                keys = tuple(item.get(k) for k in update_keys)
                args = tuple(item[c] for c in set_cols) + keys
                if self._db.execute(sql, args) == 0:
                    missed.append(keys)
        except Exception as exc:
            self._log.error("[{}] update 失败：{!r}", table, exc)
            return False
        if missed:
            # 把具体的键打出来：整批会被 dump，而没匹配上的那条回放多少次都不会成功，
            # 只报「N 条失败」的话，用户面对的就是一个反复失败的文件而不知道该改什么
            self._log.error(
                "[{}] {} 条 UPDATE 没匹配到任何行（{} 无此记录）：{}",
                table,
                len(missed),
                "、".join(update_keys),
                missed[:5],
            )
            return False
        return True

    def close(self) -> None:
        self._db.close()
