"""数据管道（阶段 03）：BasePipeline + Console / CSV / Mongo / MySQL / PostgreSQL。

需要额外依赖的管道（Mongo / MySQL / PostgreSQL）不在这里导入，按点号路径写进
``ITEM_PIPELINES`` 即可，避免没装 extra 时 ``import mineworker`` 就炸。
"""

from __future__ import annotations

from mineworker.pipelines.base import BasePipeline
from mineworker.pipelines.console import ConsolePipeline
from mineworker.pipelines.csv import CsvPipeline

__all__ = ["BasePipeline", "ConsolePipeline", "CsvPipeline"]
