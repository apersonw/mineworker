"""PostgreSQL 连接封装（psycopg 3 + psycopg_pool 连接池）。

需要 ``pip install mineworker[postgres]``。接口与 :class:`~mineworker.db.mysqldb.MysqlDB`
对齐，方便 :mod:`~mineworker.pipelines._sql` 复用同一套骨架。仅在实际实例化时才 import psycopg。
"""

from __future__ import annotations

from typing import Any

from mineworker import setting


class PostgresDB:
    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        pool_size: int | None = None,
        conninfo: str | None = None,
    ) -> None:
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - 可选依赖
            raise ImportError("PostgreSQL 支持需 pip install mineworker[postgres]") from exc

        self._dict_row = dict_row
        if conninfo is None:
            conninfo = (
                f"host={host or setting.POSTGRES_HOST} "
                f"port={int(port or setting.POSTGRES_PORT)} "
                f"user={user or setting.POSTGRES_USER} "
                f"password={password if password is not None else setting.POSTGRES_PASSWORD} "
                f"dbname={database or setting.POSTGRES_DB}"
            )
        self._pool: Any = ConnectionPool(
            conninfo,
            max_size=pool_size or setting.POSTGRES_POOL_SIZE,
            min_size=1,
            open=True,
        )

    @classmethod
    def from_url(cls, url: str) -> PostgresDB:
        """``postgresql://user:pass@host:5432/dbname``

        直接当 libpq conninfo 用，因此 ``?sslmode=require`` 之类的参数也能带。
        """
        return cls(conninfo=url)

    # ------------------------------------------------------------------
    def query(self, sql: str, args: Any = None) -> list[dict[str, Any]]:
        with self._pool.connection() as conn, conn.cursor(row_factory=self._dict_row) as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())

    def execute(self, sql: str, args: Any = None) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, args)
            return int(cur.rowcount or 0)

    def executemany(self, sql: str, args_list: Any) -> int:
        rows = list(args_list)
        if not rows:
            return 0
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(sql, rows)
            return int(cur.rowcount or 0)

    def close(self) -> None:
        self._pool.close()
