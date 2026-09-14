"""Netspy —— 一个上手简单、结构清晰的 Python 爬虫框架。

已提供：
    - `setting`               分层配置（框架默认 ← 项目 setting.py ← 环境变量 NETSPY_*）
    - `get_logger`            基于 loguru 的日志
    - 异常层级                 NetspyError 及其子类
    - `Request` / `Response`      网络层
    - `AirSpider` / `BaseParser`  轻量单机运行时（含浏览器渲染、中间件、代理池、指标、告警）
    - `Spider`                    Redis 分布式（多进程 / 多机 + 断点续爬，需 netspy[redis]）
    - `TaskSpider`                从任务源（Redis / DB）持续拉任务来爬
    - `BatchSpider`               周期性批次采集（MySQL 任务表 + 批次记录 + 进度 / 防丢）
    - `Item` / `UpdateItem`       结构化数据 + 管道落库 + 去重
"""

from __future__ import annotations

from netspy import setting
from netspy.__about__ import __version__
from netspy.core.base_parser import BaseParser
from netspy.core.spiders.air_spider import AirSpider
from netspy.core.spiders.batch_spider import BatchSpider
from netspy.core.spiders.spider import Spider
from netspy.core.spiders.task_spider import TaskSpider
from netspy.exceptions import (
    ConfigError,
    DedupError,
    ItemError,
    NetspyError,
    NotRetryError,
    PipelineError,
    RequestError,
    ResponseError,
    SpiderError,
    ValidationError,
)
from netspy.network.item import Item, UpdateItem
from netspy.network.request import Request
from netspy.network.response import Response
from netspy.network.user_pool import (
    GuestUserPool,
    LocalUserPool,
    RedisUserPool,
    User,
    UserPool,
)
from netspy.utils.log import get_logger, log

# 应用项目 setting.py 与环境变量覆盖，并按最终配置初始化日志
setting.reload()
get_logger()

__all__ = [
    "AirSpider",
    "BaseParser",
    "BatchSpider",
    "ConfigError",
    "DedupError",
    "GuestUserPool",
    "Item",
    "ItemError",
    "LocalUserPool",
    "NetspyError",
    "NotRetryError",
    "PipelineError",
    "RedisUserPool",
    "Request",
    "RequestError",
    "Response",
    "ResponseError",
    "Spider",
    "SpiderError",
    "TaskSpider",
    "UpdateItem",
    "User",
    "UserPool",
    "ValidationError",
    "__version__",
    "get_logger",
    "log",
    "setting",
]
