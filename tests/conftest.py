from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from mineworker import setting
from mineworker.utils import log


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    """每个测试前后都把配置与日志恢复到默认，隔离用例间的环境 / 配置改动。"""
    setting.reload()
    log.configure()
    yield
    setting.reload()
    log.configure()


# ----------------------------------------------------------------------
# 真实数据库集成测试用的夹具。没配环境变量就 skip —— 本地默认不拖慢，CI 里必跑。
#
#   MINEWORKER_TEST_POSTGRES_URL=postgresql://postgres:x@127.0.0.1:5432/postgres
#   MINEWORKER_TEST_MYSQL_URL=mysql://root:x@127.0.0.1:3306/mineworker
# ----------------------------------------------------------------------
def _db_url(env: str) -> str:
    url = os.environ.get(env, "").strip()
    if not url:
        pytest.skip(f"未设置 {env}，跳过真库集成测试")
    return url


@pytest.fixture
def postgres_db() -> Iterator[Any]:
    from mineworker.db.postgresdb import PostgresDB

    db = PostgresDB.from_url(_db_url("MINEWORKER_TEST_POSTGRES_URL"))
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def redis_url() -> str:
    """真 Redis 的连接串。

    分布式能力此前只用 fakeredis + 单进程测过 —— 那既不含真正的并发竞争，
    也不跨进程。这个夹具让集成测试连真实例。
    """
    return _db_url("MINEWORKER_TEST_REDIS_URL")


@pytest.fixture
def mysql_db() -> Iterator[Any]:
    from mineworker.db.mysqldb import MysqlDB

    db = MysqlDB.from_url(_db_url("MINEWORKER_TEST_MYSQL_URL"))
    try:
        yield db
    finally:
        db.close()
