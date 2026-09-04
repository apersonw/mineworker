"""MineWorker —— 一个上手简单、结构清晰的 Python 爬虫框架（对标 feapder）。

已提供：
    - `setting`               分层配置（框架默认 ← 项目 setting.py ← 环境变量 MINEWORKER_*）
    - `get_logger`            基于 loguru 的日志
    - 异常层级                 MineWorkerError 及其子类
    - `Request` / `Response`      网络层
    - `AirSpider` / `BaseParser`  轻量单机运行时（含浏览器渲染、中间件、代理池、指标、告警）
    - `Spider`                    Redis 分布式（多进程 / 多机 + 断点续爬，需 mineworker[redis]）
    - `TaskSpider`                从任务源（Redis / DB）持续拉任务来爬
    - `Item` / `UpdateItem`       结构化数据 + 管道落库 + 去重
"""

from __future__ import annotations

from mineworker import setting
from mineworker.__about__ import __version__
from mineworker.core.base_parser import BaseParser
from mineworker.core.spiders.air_spider import AirSpider
from mineworker.core.spiders.spider import Spider
from mineworker.core.spiders.task_spider import TaskSpider
from mineworker.exceptions import (
    ConfigError,
    DedupError,
    ItemError,
    MineWorkerError,
    NotRetryError,
    PipelineError,
    RequestError,
    ResponseError,
    SpiderError,
    ValidationError,
)
from mineworker.network.item import Item, UpdateItem
from mineworker.network.request import Request
from mineworker.network.response import Response
from mineworker.utils.log import get_logger, log

# 应用项目 setting.py 与环境变量覆盖，并按最终配置初始化日志
setting.reload()
get_logger()

__all__ = [
    "AirSpider",
    "BaseParser",
    "ConfigError",
    "DedupError",
    "Item",
    "ItemError",
    "MineWorkerError",
    "NotRetryError",
    "PipelineError",
    "Request",
    "RequestError",
    "Response",
    "ResponseError",
    "Spider",
    "SpiderError",
    "TaskSpider",
    "UpdateItem",
    "ValidationError",
    "__version__",
    "get_logger",
    "log",
    "setting",
]
