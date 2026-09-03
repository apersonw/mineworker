from __future__ import annotations

from pathlib import Path

import pytest

from mineworker import setting
from mineworker.utils import log


def test_get_logger_is_usable() -> None:
    lg = log.get_logger("test")
    lg.info("hello")  # 不应抛异常


def test_configure_writes_to_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logfile = tmp_path / "mw.log"
    monkeypatch.setattr(setting, "LOG_FILE", str(logfile))
    log.configure()
    log.get_logger("t").warning("写到文件")
    assert logfile.exists()
    assert "写到文件" in logfile.read_text(encoding="utf-8")


def test_level_filters_lower_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logfile = tmp_path / "mw.log"
    monkeypatch.setattr(setting, "LOG_FILE", str(logfile))
    monkeypatch.setattr(setting, "LOG_LEVEL", "WARNING")
    log.configure()
    log.get_logger("t").info("看不见")
    log.get_logger("t").error("看得见")
    body = logfile.read_text(encoding="utf-8")
    assert "看不见" not in body
    assert "看得见" in body
