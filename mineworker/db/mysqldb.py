"""MySQL 连接封装（pymysql + DBUtils 连接池）。

需要 ``pip install mineworker[mysql]``。仅在实际实例化 ``MysqlDB`` 时才 import pymysql。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import unquote, urlsplit

from mineworker import setting


class MysqlDB:
    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        charset: str = "utf8mb4",
        pool_size: int | None = None,
    ) -> None:
        try:
            import pymysql
            from dbutils.pooled_db import PooledDB
        except ImportError as exc:  # pragma: no cover - 可选依赖
            raise ImportError("MySQL 支持需 pip install mineworker[mysql]") from exc

        self._pool: Any = PooledDB(
            creator=pymysql,
            maxconnections=pool_size or setting.MYSQL_POOL_SIZE,
            mincached=1,
            blocking=True,
            host=host or setting.MYSQL_HOST,
            port=int(port or setting.MYSQL_PORT),
            user=user or setting.MYSQL_USER,
            password=password if password is not None else setting.MYSQL_PASSWORD,
            database=database or setting.MYSQL_DB,
            charset=charset,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )

    @classmethod
    def from_url(cls, url: str) -> MysqlDB:
        """``mysql://user:pass@host:3306/dbname``"""
        parts = urlsplit(url)
        return cls(
            host=parts.hostname or None,
            port=parts.port or None,
            user=unquote(parts.username) if parts.username else None,
            password=unquote(parts.password) if parts.password else None,
            database=parts.path.lstrip("/") or None,
        )

    # ------------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """在**同一条连接**上跑多条语句，出错回滚。

        普通的 `query` / `execute` 各自从池里借一条连接、用完就还，而且池是
        ``autocommit=True`` 的 —— 所以「先 SELECT ... FOR UPDATE 再 UPDATE」
        这种写法在它们身上**根本锁不住**：两条语句在两条连接、两个事务里，
        行锁在第一条语句结束时就放掉了。

        需要「读到什么就锁住什么」的地方（比如多 worker 抢任务）必须用这个。
        """
        conn = self._pool.connection()
        try:
            conn.begin()
            yield conn
            conn.commit()
        except BaseException:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise
        finally:
            conn.close()

    def query(self, sql: str, args: Any = None) -> list[dict[str, Any]]:
        conn = self._pool.connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return list(cur.fetchall())
        finally:
            conn.close()

    def execute(self, sql: str, args: Any = None) -> int:
        conn = self._pool.connection()
        try:
            with conn.cursor() as cur:
                affected = cur.execute(sql, args)
            conn.commit()
            return int(affected or 0)
        finally:
            conn.close()

    def insert(self, sql: str, args: Any = None) -> int:
        """执行一条 INSERT，返回自增主键（``cursor.lastrowid``）。"""
        conn = self._pool.connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                rowid = cur.lastrowid
            conn.commit()
            return int(rowid or 0)
        finally:
            conn.close()

    def executemany(self, sql: str, args_list: Any) -> int:
        rows = list(args_list)
        if not rows:
            return 0
        conn = self._pool.connection()
        try:
            with conn.cursor() as cur:
                affected = cur.executemany(sql, rows)
            conn.commit()
            return int(affected or 0)
        finally:
            conn.close()

    def close(self) -> None:
        self._pool.close()
