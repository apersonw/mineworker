from __future__ import annotations

from pathlib import Path

import pytest

from netspy import setting
from netspy.utils import log


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


# ======================================================================
# LoggerMixin —— 混入它就有 self.logger，不用自己 import / get_logger。
#
# 没有它之前，写一个爬虫想打日志得自己 `from netspy.utils.log import
# get_logger` 再手动 bind 一个名字，还常常图省事直接开在模块级（一个全局
# 变量，和具体类没绑定关系）。这里既测 mixin 本身，也逐个测四个真正混入了
# 它的基类——用户实际写的是这四个的子类，只在 mixin 层面测不出「忘了往
# 某个基类身上加」这种漏网。
# ======================================================================
def test_logger_mixin_binds_the_subclass_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logfile = tmp_path / "mw.log"
    monkeypatch.setattr(setting, "LOG_FILE", str(logfile))
    log.configure()

    class BookSpider(log.LoggerMixin):
        pass

    BookSpider().logger.info("抓到一本书")
    body = logfile.read_text(encoding="utf-8")
    assert "抓到一本书" in body
    assert "BookSpider" in body, "日志里该看到类名，不是随便一个全局名字"


def test_logger_mixin_distinguishes_different_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个不同的子类各自打一行——日志里得分得清是谁打的。"""
    logfile = tmp_path / "mw.log"
    monkeypatch.setattr(setting, "LOG_FILE", str(logfile))
    log.configure()

    class Alpha(log.LoggerMixin):
        pass

    class Beta(log.LoggerMixin):
        pass

    Alpha().logger.info("来自 Alpha")
    Beta().logger.info("来自 Beta")
    lines = logfile.read_text(encoding="utf-8").splitlines()
    alpha_line = next(line for line in lines if "来自 Alpha" in line)
    beta_line = next(line for line in lines if "来自 Beta" in line)
    assert "Alpha" in alpha_line and "Beta" not in alpha_line
    assert "Beta" in beta_line and "Alpha" not in beta_line


def test_base_parser_subclasses_get_logger_with_zero_setup() -> None:
    """写一个爬虫，`self.logger` 直接能用——不用覆写 `__init__`，不用 import。"""
    from netspy.core.base_parser import BaseParser

    class DemoSpider(BaseParser):
        pass

    DemoSpider().logger.info("spider 直接能打日志")  # 不该抛异常


def test_base_pipeline_subclasses_get_logger_with_zero_setup() -> None:
    from netspy.pipelines.base import BasePipeline

    class DemoPipeline(BasePipeline):
        def save_items(self, table: str, items: list[dict[str, object]]) -> bool:
            self.logger.info("写了 {} 条到 {}", len(items), table)
            return True

    assert DemoPipeline().save_items("t", [{"a": 1}]) is True


def test_downloader_middleware_subclasses_get_logger_with_zero_setup() -> None:
    from netspy.network.middleware import DownloaderMiddleware

    class DemoMiddleware(DownloaderMiddleware):
        pass

    DemoMiddleware().logger.info("middleware 直接能打日志")


def test_user_pool_subclasses_get_logger_with_zero_setup() -> None:
    from netspy.network.user_pool.base import User, UserPool

    class DemoPool(UserPool):
        def get(self) -> User | None:
            self.logger.info("借号")
            return None

    assert DemoPool().get() is None
