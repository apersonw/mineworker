"""无状态工具函数：哈希 / 指纹、JSON、动态导入、时间、重试装饰器。"""

from __future__ import annotations

import functools
import hashlib
import importlib
import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

_T = TypeVar("_T")


def md5(text: str | bytes) -> str:
    """返回 32 位十六进制 MD5（仅用于指纹，不用于安全场景）。"""
    data = text.encode() if isinstance(text, str) else text
    return hashlib.md5(data).hexdigest()


def get_fingerprint(*parts: Any) -> str:
    """对任意组成部分生成稳定指纹。dict / list 会按键排序后序列化。"""
    joined = "|".join(_stringify(p) for p in parts)
    return md5(joined)


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return str(value)


def dumps_json(obj: Any, *, indent: int | None = None, sort_keys: bool = False) -> str:
    """`json.dumps` 的常用默认值封装：不转义非 ASCII，非常规对象回退到 str。"""
    return json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=sort_keys, default=str)


def loads_json(text: str | bytes) -> Any:
    return json.loads(text)


def load_object(path: str) -> Any:
    """按点号路径导入对象，如 ``mineworker.pipelines.console.ConsolePipeline``。"""
    if "." not in path:
        raise ValueError(f"不是合法的对象路径：{path!r}")
    module_path, _, name = path.rpartition(".")
    module = importlib.import_module(module_path)
    try:
        return getattr(module, name)
    except AttributeError as exc:
        raise ImportError(f"模块 {module_path!r} 中没有 {name!r}") from exc


def retry(
    times: int = 3,
    *,
    interval: float = 0.0,
    exceptions: type[BaseException] | tuple[type[BaseException], ...] = Exception,
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """同步重试装饰器：首次失败后最多再试 ``times`` 次，全部失败则抛出最后一次异常。"""

    def decorator(func: Callable[..., _T]) -> Callable[..., _T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> _T:
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except exceptions:
                    attempt += 1
                    if attempt > times:
                        raise
                    if interval:
                        time.sleep(interval)

        return wrapper

    return decorator


def now() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(tz=timezone.utc)


def format_date(dt: datetime | None = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return (dt or now()).strftime(fmt)


def current_timestamp() -> int:
    return int(time.time())
