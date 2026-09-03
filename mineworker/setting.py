"""框架默认配置 + 分层加载。

优先级（后者覆盖前者）：

    1. 本模块定义的框架默认值
    2. 项目配置文件：环境变量 ``MINEWORKER_SETTING`` 指定的 .py 文件，
       否则当前工作目录下的 ``setting.py`` / ``settings.py``
    3. 环境变量 ``MINEWORKER_<KEY>``（按默认值类型自动转换）

``import mineworker`` 时会自动执行一次 :func:`reload`。测试或运行期改动了
环境 / 配置文件后，再次调用 :func:`reload` 即可重新应用；:func:`apply` 用于
合并爬虫的 ``__custom_setting__``。
"""

from __future__ import annotations

import copy
import json
import os
import runpy
import warnings
from pathlib import Path
from typing import Any

# ======================================================================
# 框架默认值
# ======================================================================
PROJECT_NAME: str = "mineworker"

# ---- 日志 ----
LOG_LEVEL: str = "INFO"
LOG_FILE: str | None = None
LOG_COLOR: bool = True
LOG_ROTATION: str = "50 MB"
LOG_RETENTION: str = "10 days"

# ---- 调度 / 运行时 ----
SPIDER_THREAD_COUNT: int = 4
SPIDER_MAX_RETRY_TIMES: int = 3
SPIDER_RETRY_INTERVAL: float = 0.0  # 重试前等待（秒）
COLLECTOR_TASK_COUNT: int = 100  # collector 单次从队列取多少任务
REQUEST_BUFFER_MAX_CACHED: int = 1000  # RequestBuffer 达到此量立即 flush
BUFFER_FLUSH_INTERVAL: float = 0.1  # RequestBuffer / ItemBuffer flush 轮询间隔
DONE_CHECK_TIMES: int = 3  # 结束检测连续复查次数
DONE_CHECK_INTERVAL: float = 0.5  # 每次复查间隔（秒）
DUMP_UNFINISHED_ON_EXIT: bool = True  # 中断退出时把未完成请求 dump 到 FAILED_REQUEST_PATH

# ---- 请求 ----
REQUEST_TIMEOUT: float = 22.0
RANDOM_USER_AGENT: bool = True
USE_SESSION: bool = False

# ---- Item / 管道 ----
ITEM_MAX_CACHED_COUNT: int = 5000  # ItemBuffer 达到此量立即 flush
ITEM_PIPELINES: list[str] = ["mineworker.pipelines.console.ConsolePipeline"]
ITEM_DEFAULT_TABLE: str = "items"  # 直接 yield dict（非 Item）时落库的表名
ITEM_FILTER_ENABLE: bool = True  # 是否对 Item 做去重（按 fingerprint）
CSV_OUTPUT_DIR: str = "."  # CsvPipeline 输出目录
FAILED_ITEM_PATH: str = "failed_items.jsonl"
FAILED_REQUEST_PATH: str = "failed_requests.jsonl"

# ---- 去重 ----
DEDUP_FILTER: str = "memory"  # memory（布隆）| lite（精确 set）
DEDUP_TO_MD5: bool = True  # Dedup 直接传入原始值时是否先 md5
DEDUP_ERROR_RATE: float = 1e-6
DEDUP_INITIAL_CAPACITY: int = 1_000_000

# ---- MongoDB ----
MONGO_URI: str = "mongodb://localhost:27017"
MONGO_DB: str = "mineworker"

# ---- Redis（Roadmap v2 预留）----
REDIS_URL: str = "redis://localhost:6379/0"

# ---- 浏览器渲染 ----
WEBDRIVER: dict[str, Any] = {
    "pool_size": 1,
    "headless": True,
    "load_images": False,
    "timeout": 30,
    "render_time": 0,
    "user_agent": None,
    "proxy": None,
}

# ---- 告警 ----
WARNING_FEISHU_WEBHOOK: str = ""
WARNING_INTERVAL: float = 300.0
WARNING_FAILED_RATE: float = 0.5
WARNING_FAILED_COUNT: int = 1000


# ======================================================================
# 加载机制
# ======================================================================
_SETTING_KEYS: frozenset[str] = frozenset(
    name for name in tuple(globals()) if name.isupper() and not name.startswith("_")
)
_DEFAULTS: dict[str, Any] = {name: copy.deepcopy(globals()[name]) for name in _SETTING_KEYS}


def _coerce(current: Any, raw: str) -> Any:
    """把环境变量字符串按 `current` 的类型转换。"""
    if isinstance(current, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, (list, dict)):
        return json.loads(raw)
    return raw


def _apply(mapping: dict[str, Any]) -> None:
    g = globals()
    for key, value in mapping.items():
        if key.isupper() and not key.startswith("_"):
            g[key] = value


def _load_project_file() -> dict[str, Any]:
    override = os.environ.get("MINEWORKER_SETTING")
    if override:
        candidates = [Path(override)]
    else:
        cwd = Path.cwd()
        candidates = [cwd / "setting.py", cwd / "settings.py"]

    for path in candidates:
        if not path.is_file():
            continue
        try:
            namespace = runpy.run_path(str(path))
        except Exception as exc:  # 配置文件可能有任意错误
            warnings.warn(f"加载项目配置 {path} 失败：{exc!r}", stacklevel=3)
            return {}
        return {k: v for k, v in namespace.items() if k.isupper() and not k.startswith("_")}
    return {}


def _apply_env() -> None:
    g = globals()
    for key in _SETTING_KEYS:
        env_key = f"MINEWORKER_{key}"
        raw = os.environ.get(env_key)
        if raw is None:
            continue
        try:
            g[key] = _coerce(_DEFAULTS[key], raw)
        except ValueError as exc:  # int/float/json.loads 解析失败
            warnings.warn(f"环境变量 {env_key} 解析失败：{exc!r}", stacklevel=3)


def reload() -> None:
    """重置为默认值，再依次应用项目配置文件与环境变量。"""
    _apply({name: copy.deepcopy(value) for name, value in _DEFAULTS.items()})
    _apply(_load_project_file())
    _apply_env()


def apply(mapping: dict[str, Any]) -> None:
    """合并额外配置（供 Spider 的 ``__custom_setting__`` 使用）。"""
    _apply({k: v for k, v in mapping.items() if k.isupper() and not k.startswith("_")})


def as_dict() -> dict[str, Any]:
    """返回当前全部配置项的快照。"""
    g = globals()
    return {key: g[key] for key in sorted(_SETTING_KEYS)}
