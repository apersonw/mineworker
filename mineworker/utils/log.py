"""基于 loguru 的日志封装。

`get_logger()` 在首次调用时按当前 `mineworker.setting` 配置初始化 sink；
配置变更后可调用 `configure()` 重新初始化。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from loguru import logger

from mineworker import setting

if TYPE_CHECKING:
    from loguru import Logger

#: 未绑定 name 的全局 logger，等价于 loguru 的 logger
log = logger

_state: dict[str, bool] = {"configured": False}

_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <7}</level> | "
    "<cyan>{extra[name]}</cyan> - <level>{message}</level>"
)


def configure() -> None:
    """按 `mineworker.setting` 的当前值重建日志 sink。"""
    logger.remove()
    logger.configure(extra={"name": setting.PROJECT_NAME})
    logger.add(
        sys.stderr,
        level=setting.LOG_LEVEL,
        colorize=setting.LOG_COLOR,
        format=_FORMAT,
    )
    log_file = setting.LOG_FILE
    if log_file:
        logger.add(
            log_file,
            level=setting.LOG_LEVEL,
            rotation=setting.LOG_ROTATION,
            retention=setting.LOG_RETENTION,
            encoding="utf-8",
            format=_FORMAT,
        )
    _state["configured"] = True


def get_logger(name: str | None = None) -> Logger:
    """返回 logger；`name` 会作为 `{extra[name]}` 显示在日志中。"""
    if not _state["configured"]:
        configure()
    return logger.bind(name=name) if name else logger
