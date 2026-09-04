"""``BatchSpider`` 的持久层：任务表状态机 + 批次记录表。

任务表的状态列（默认列名 ``batch_status``）取值：

===== ==========
  值   含义
===== ==========
  0    待处理
  1    已完成
  2    处理中
 -1    失败（不再重试）
===== ==========

:class:`BatchStore` 是抽象接口；:class:`MysqlBatchStore` 落 MySQL，
:class:`MemoryBatchStore` 供测试 / 小规模内存跑批。
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mineworker.utils import tools

if TYPE_CHECKING:
    from mineworker.db.mysqldb import MysqlDB

TODO = 0
DONE = 1
DOING = 2
FAILED = -1


@dataclass
class BatchRecord:
    id: int
    batch_date: str
    total_count: int
    done_count: int
    fail_count: int
    interval: float
    interval_unit: str
    is_done: bool


@dataclass
class TaskCounts:
    total: int = 0
    todo: int = 0
    doing: int = 0
    done: int = 0
    failed: int = 0

    @property
    def settled(self) -> int:
        return self.done + self.failed

    @property
    def finished(self) -> bool:
        return self.total > 0 and self.settled >= self.total


class BatchStore(abc.ABC):
    # ---- 批次记录表 ----
    @abc.abstractmethod
    def ensure_schema(self) -> None:
        """建批次记录表（幂等）。任务表由用户自行创建。"""

    @abc.abstractmethod
    def latest_batch(self) -> BatchRecord | None:
        """最近一个批次（不管是否完成），没有则 None。"""

    @abc.abstractmethod
    def batch_age_seconds(self, batch_id: int) -> float:
        """批次创建至今的秒数。"""

    @abc.abstractmethod
    def create_batch(
        self, batch_date: str, total: int, interval: float, unit: str
    ) -> BatchRecord: ...

    @abc.abstractmethod
    def update_batch_counts(self, batch_id: int, *, done: int, failed: int, total: int) -> None: ...

    @abc.abstractmethod
    def finish_batch(self, batch_id: int) -> None: ...

    # ---- 任务表 ----
    @abc.abstractmethod
    def count_tasks(self) -> TaskCounts: ...

    @abc.abstractmethod
    def reset_all_tasks(self) -> None:
        """所有任务状态 → 待处理（开新批次时调用）。"""

    @abc.abstractmethod
    def reset_lost_tasks(self, stale_seconds: float) -> int:
        """「处理中」且超过 ``stale_seconds`` 没更新的任务 → 待处理；返回重置数量。"""

    @abc.abstractmethod
    def claim_tasks(self, limit: int) -> list[dict[str, Any]]:
        """认领至多 ``limit`` 个待处理任务（状态置为处理中），返回整行。"""

    @abc.abstractmethod
    def mark_task(self, task_id: Any, state: int) -> None:
        """回写单个任务状态（worker 解析完成 / 失败后调用）。"""

    def close(self) -> None:  # noqa: B027 - 可选钩子
        """释放连接。"""


# ======================================================================
# 内存实现（测试 / 小规模）
# ======================================================================
class MemoryBatchStore(BatchStore):
    def __init__(
        self,
        tasks: list[dict[str, Any]],
        *,
        id_field: str = "id",
        state_field: str = "batch_status",
    ) -> None:
        self._id_field = id_field
        self._state_field = state_field
        self._tasks: list[dict[str, Any]] = []
        for task in tasks:
            row = dict(task)
            row.setdefault(state_field, TODO)
            self._tasks.append(row)
        self._touched: dict[Any, float] = {}
        self._batches: list[BatchRecord] = []
        self._created_at: dict[int, float] = {}
        self._seq = 0

    # ---- 批次记录 ----
    def ensure_schema(self) -> None:
        pass

    def latest_batch(self) -> BatchRecord | None:
        return self._batches[-1] if self._batches else None

    def batch_age_seconds(self, batch_id: int) -> float:
        return time.time() - self._created_at.get(batch_id, time.time())

    def create_batch(self, batch_date: str, total: int, interval: float, unit: str) -> BatchRecord:
        self._seq += 1
        record = BatchRecord(
            id=self._seq,
            batch_date=batch_date,
            total_count=total,
            done_count=0,
            fail_count=0,
            interval=interval,
            interval_unit=unit,
            is_done=False,
        )
        self._batches.append(record)
        self._created_at[record.id] = time.time()
        return record

    def update_batch_counts(self, batch_id: int, *, done: int, failed: int, total: int) -> None:
        for record in self._batches:
            if record.id == batch_id:
                record.done_count, record.fail_count, record.total_count = done, failed, total

    def finish_batch(self, batch_id: int) -> None:
        for record in self._batches:
            if record.id == batch_id:
                record.is_done = True

    # ---- 任务表 ----
    def _state(self, row: dict[str, Any]) -> int:
        return int(row.get(self._state_field, TODO))

    def count_tasks(self) -> TaskCounts:
        counts = TaskCounts(total=len(self._tasks))
        buckets = {TODO: "todo", DONE: "done", DOING: "doing", FAILED: "failed"}
        for row in self._tasks:
            attr = buckets.get(self._state(row))
            if attr is not None:
                setattr(counts, attr, getattr(counts, attr) + 1)
        return counts

    def reset_all_tasks(self) -> None:
        for row in self._tasks:
            row[self._state_field] = TODO
        self._touched.clear()

    def reset_lost_tasks(self, stale_seconds: float) -> int:
        now = time.time()
        reset = 0
        for row in self._tasks:
            tid = row[self._id_field]
            if self._state(row) == DOING and now - self._touched.get(tid, now) >= stale_seconds:
                row[self._state_field] = TODO
                reset += 1
        return reset

    def claim_tasks(self, limit: int) -> list[dict[str, Any]]:
        claimed: list[dict[str, Any]] = []
        for row in self._tasks:
            if len(claimed) >= max(1, limit):
                break
            if self._state(row) == TODO:
                row[self._state_field] = DOING
                self._touched[row[self._id_field]] = time.time()
                claimed.append(dict(row))
        return claimed

    def mark_task(self, task_id: Any, state: int) -> None:
        for row in self._tasks:
            if row[self._id_field] == task_id:
                row[self._state_field] = state
                self._touched[task_id] = time.time()
                return


# ======================================================================
# MySQL 实现
# ======================================================================
class MysqlBatchStore(BatchStore):
    def __init__(
        self,
        task_table: str,
        *,
        db: MysqlDB | None = None,
        id_field: str = "id",
        state_field: str = "batch_status",
        time_field: str = "update_time",
    ) -> None:
        if db is None:
            from mineworker.db.mysqldb import MysqlDB

            db = MysqlDB()
        self._db = db
        self._task_table = task_table
        self._record_table = f"{task_table}_batch_record"
        self._id = id_field
        self._state = state_field
        self._time = time_field

    # ---- 批次记录 ----
    def ensure_schema(self) -> None:
        self._db.execute(
            f"CREATE TABLE IF NOT EXISTS `{self._record_table}` ("
            "`id` BIGINT NOT NULL AUTO_INCREMENT,"
            "`batch_date` VARCHAR(40) NOT NULL,"
            "`total_count` INT NOT NULL DEFAULT 0,"
            "`done_count` INT NOT NULL DEFAULT 0,"
            "`fail_count` INT NOT NULL DEFAULT 0,"
            "`interval` DOUBLE NOT NULL DEFAULT 0,"
            "`interval_unit` VARCHAR(10) NOT NULL DEFAULT 'day',"
            "`is_done` TINYINT NOT NULL DEFAULT 0,"
            "`create_time` DATETIME NOT NULL,"
            "`update_time` DATETIME NOT NULL,"
            "PRIMARY KEY (`id`)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )

    def latest_batch(self) -> BatchRecord | None:
        rows = self._db.query(f"SELECT * FROM `{self._record_table}` ORDER BY `id` DESC LIMIT 1")
        return self._to_record(rows[0]) if rows else None

    def batch_age_seconds(self, batch_id: int) -> float:
        rows = self._db.query(
            f"SELECT TIMESTAMPDIFF(SECOND, `create_time`, NOW()) AS age "
            f"FROM `{self._record_table}` WHERE `id` = %s",
            (batch_id,),
        )
        return float(rows[0]["age"]) if rows and rows[0]["age"] is not None else 0.0

    def create_batch(self, batch_date: str, total: int, interval: float, unit: str) -> BatchRecord:
        cols = (
            "`batch_date`, `total_count`, `interval`, `interval_unit`, `create_time`, `update_time`"
        )
        batch_id = self._db.insert(
            f"INSERT INTO `{self._record_table}` ({cols}) VALUES (%s, %s, %s, %s, NOW(), NOW())",
            (batch_date, total, interval, unit),
        )
        return BatchRecord(batch_id, batch_date, total, 0, 0, interval, unit, is_done=False)

    def update_batch_counts(self, batch_id: int, *, done: int, failed: int, total: int) -> None:
        self._db.execute(
            f"UPDATE `{self._record_table}` SET `done_count`=%s, `fail_count`=%s, "
            "`total_count`=%s, `update_time`=NOW() WHERE `id`=%s",
            (done, failed, total, batch_id),
        )

    def finish_batch(self, batch_id: int) -> None:
        self._db.execute(
            f"UPDATE `{self._record_table}` SET `is_done`=1, `update_time`=NOW() WHERE `id`=%s",
            (batch_id,),
        )

    # ---- 任务表 ----
    def count_tasks(self) -> TaskCounts:
        rows = self._db.query(
            f"SELECT `{self._state}` AS s, COUNT(*) AS c FROM `{self._task_table}` "
            f"GROUP BY `{self._state}`"
        )
        by_state = {int(r["s"]): int(r["c"]) for r in rows}
        return TaskCounts(
            total=sum(by_state.values()),
            todo=by_state.get(TODO, 0),
            doing=by_state.get(DOING, 0),
            done=by_state.get(DONE, 0),
            failed=by_state.get(FAILED, 0),
        )

    def reset_all_tasks(self) -> None:
        self._db.execute(f"UPDATE `{self._task_table}` SET `{self._state}`={TODO}")

    def reset_lost_tasks(self, stale_seconds: float) -> int:
        return self._db.execute(
            f"UPDATE `{self._task_table}` SET `{self._state}`={TODO} "
            f"WHERE `{self._state}`={DOING} "
            f"AND `{self._time}` < DATE_SUB(NOW(), INTERVAL %s SECOND)",
            (int(stale_seconds),),
        )

    def claim_tasks(self, limit: int) -> list[dict[str, Any]]:
        rows = self._db.query(
            f"SELECT * FROM `{self._task_table}` WHERE `{self._state}`={TODO} LIMIT %s",
            (max(1, limit),),
        )
        if not rows:
            return []
        ids = [r[self._id] for r in rows]
        placeholders = ", ".join(["%s"] * len(ids))
        self._db.execute(
            f"UPDATE `{self._task_table}` SET `{self._state}`={DOING} "
            f"WHERE `{self._id}` IN ({placeholders})",
            tuple(ids),
        )
        return rows

    def mark_task(self, task_id: Any, state: int) -> None:
        self._db.execute(
            f"UPDATE `{self._task_table}` SET `{self._state}`=%s WHERE `{self._id}`=%s",
            (state, task_id),
        )

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------------
    @staticmethod
    def _to_record(row: dict[str, Any]) -> BatchRecord:
        return BatchRecord(
            id=int(row["id"]),
            batch_date=str(row["batch_date"]),
            total_count=int(row["total_count"]),
            done_count=int(row["done_count"]),
            fail_count=int(row["fail_count"]),
            interval=float(row["interval"]),
            interval_unit=str(row["interval_unit"]),
            is_done=bool(row["is_done"]),
        )


def default_batch_date() -> str:
    return tools.format_date()
