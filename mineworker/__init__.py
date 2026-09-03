"""MineWorker —— 一个上手简单、结构清晰的 Python 爬虫框架（对标 feapder）。

已提供：
    - `setting`             分层配置（框架默认 ← 项目 setting.py ← 环境变量 MINEWORKER_*）
    - `get_logger`          基于 loguru 的日志
    - 异常层级               MineWorkerError 及其子类
    - `Request` / `Response`  网络层（阶段 01）

后续阶段将在此导出：
    阶段 02  AirSpider
    阶段 03  Item / UpdateItem
"""

from __future__ import annotations

from mineworker import setting
from mineworker.__about__ import __version__
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
from mineworker.network.request import Request
from mineworker.network.response import Response
from mineworker.utils.log import get_logger, log

# 应用项目 setting.py 与环境变量覆盖，并按最终配置初始化日志
setting.reload()
get_logger()

__all__ = [
    "ConfigError",
    "DedupError",
    "ItemError",
    "MineWorkerError",
    "NotRetryError",
    "PipelineError",
    "Request",
    "RequestError",
    "Response",
    "ResponseError",
    "SpiderError",
    "ValidationError",
    "__version__",
    "get_logger",
    "log",
    "setting",
]
