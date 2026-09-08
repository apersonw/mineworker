"""「当前正在处理的请求」——供框架代码在用户回调执行期间取用。

用来让照文档写的爬虫**自动变对**：`BatchSpider.update_task()` 得知道自己是在
处理哪个请求，才能把任务状态的写入推迟到那批数据真正落库之后。
要求用户多传一个参数是不行的 —— 已经照旧文档写好的爬虫不会跟着改。

必须是线程本地的：worker 是多线程的，一个全局变量会让 A 线程的
`update_task` 挂到 B 线程的请求上。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mineworker.network.request import Request

_local = threading.local()


def set_current(request: Request | None, item_buffer: Any = None) -> None:
    _local.request = request
    _local.item_buffer = item_buffer


def get_current_request() -> Any:
    return getattr(_local, "request", None)


def get_current_item_buffer() -> Any:
    return getattr(_local, "item_buffer", None)
