"""框架异常层级。所有自定义异常均继承 `MineWorkerError`。"""

from __future__ import annotations


class MineWorkerError(Exception):
    """所有 MineWorker 异常的基类。"""


class ConfigError(MineWorkerError):
    """配置缺失或非法。"""


class SpiderError(MineWorkerError):
    """爬虫定义或运行期错误。"""


class RequestError(MineWorkerError):
    """请求下载失败（网络异常、超时、状态码不符合预期等）。"""


class HttpStatusError(RequestError):
    """响应状态码不被接受（非 2xx/3xx，且不在 `ACCEPT_STATUS_CODES` 里）。

    继承 `RequestError`，因此复用既有的下载失败重试路径与 `mineworker retry` 回放。
    """

    def __init__(self, status_code: int, url: str = "") -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}{f'：{url}' if url else ''}")


class AntiBotError(RequestError):
    """响应疑似反爬拦截页（Cloudflare / Akamai 挑战页等）。

    继承 `RequestError`，因此会走既有的下载失败重试路径：重试时代理池会换一个出口。
    """


class ResponseError(MineWorkerError):
    """响应内容异常（解析失败、非预期结构等）。"""


class ValidationError(MineWorkerError):
    """`validate()` 校验不通过；触发重试或丢弃。"""


class NotRetryError(MineWorkerError):
    """在解析中抛出以放弃当前请求，跳过剩余重试。"""


class ItemError(MineWorkerError):
    """Item 定义或序列化错误。"""


class PipelineError(MineWorkerError):
    """管道写入失败。"""


class DedupError(MineWorkerError):
    """去重过滤器错误。"""
