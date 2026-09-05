"""写入 PostgreSQL。需要 ``pip install mineworker[postgres]``。

与 MySQL 的关键差异在 upsert：MySQL 的 ``ON DUPLICATE KEY UPDATE`` 不用指明冲突键，
Postgres 的 ``ON CONFLICT`` 必须给冲突目标。因此用两个设置表达：

``POSTGRES_ON_CONFLICT``
    ``error`` 裸 INSERT（冲突即报错）/ ``nothing`` 冲突则跳过（默认，对爬虫最安全）/
    ``update`` 冲突则更新，需要 ``POSTGRES_CONFLICT_TARGET``

``POSTGRES_CONFLICT_TARGET``
    ``update`` 模式下的冲突列（通常是唯一索引的列）。留空会降级成 ``DO NOTHING``
    并告警一次 —— 宁可少写几条，也好过整批抛异常。
"""

from __future__ import annotations

from typing import Any

from mineworker import setting
from mineworker.pipelines._sql import SqlPipeline


class PostgresPipeline(SqlPipeline):
    log_name = "pipeline.postgres"

    def __init__(self, *, db: Any = None, **conn: Any) -> None:
        if db is None:
            from mineworker.db.postgresdb import PostgresDB

            db = PostgresDB(**conn)
        super().__init__(db)
        self._warned_no_target = False

    def _quote(self, ident: str) -> str:
        return '"{}"'.format(ident.replace('"', '""'))

    def _conflict_clause(self, cols: list[str]) -> str:
        mode = setting.POSTGRES_ON_CONFLICT
        if mode == "error":
            return ""
        if mode == "update":
            target = list(setting.POSTGRES_CONFLICT_TARGET)
            if target:
                target_sql = ", ".join(self._quote(c) for c in target)
                # 冲突目标列本身不该出现在 SET 里
                upd_cols = [c for c in cols if c not in target]
                if upd_cols:
                    upd = ", ".join(f"{self._quote(c)}=EXCLUDED.{self._quote(c)}" for c in upd_cols)
                    return f" ON CONFLICT ({target_sql}) DO UPDATE SET {upd}"
                return f" ON CONFLICT ({target_sql}) DO NOTHING"
            if not self._warned_no_target:
                self._log.warning(
                    "POSTGRES_ON_CONFLICT=update 但 POSTGRES_CONFLICT_TARGET 为空，"
                    "本次降级为 DO NOTHING；请填写冲突列（通常是唯一索引的列）"
                )
                self._warned_no_target = True
        return " ON CONFLICT DO NOTHING"
