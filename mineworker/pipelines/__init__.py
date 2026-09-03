"""数据管道（阶段 03）：BasePipeline + Console / CSV / Mongo。"""

from __future__ import annotations

from mineworker.pipelines.base import BasePipeline
from mineworker.pipelines.console import ConsolePipeline
from mineworker.pipelines.csv import CsvPipeline

__all__ = ["BasePipeline", "ConsolePipeline", "CsvPipeline"]
