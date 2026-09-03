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
