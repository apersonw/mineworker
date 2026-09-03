from __future__ import annotations

from collections.abc import Iterator

import pytest

from mineworker import setting
from mineworker.utils import log


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    """每个测试前后都把配置与日志恢复到默认，隔离用例间的环境 / 配置改动。"""
    setting.reload()
    log.configure()
    yield
    setting.reload()
    log.configure()
