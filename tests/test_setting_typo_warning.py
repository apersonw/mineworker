"""配置名写错时要出声 —— 但只在「像是拼错」时。

`_apply()` 把任何大写键塞进模块全局，从不与 `_SETTING_KEYS` 比对（那个集合就在
同一个文件里，只用于重置和导出）。于是 `setting.py` 里少写一个字母，配置
**静默生效为零、零提示**。

这不是假想的：做全栈集成测试时我自己两次踩中 —— 先写了 `POSTGRES_URL`
（真名是分字段的 `POSTGRES_HOST` / `_USER` / …），表现成「数据库认证失败」
而不是「配置名写错了」；又写了 `REQUEST_FILTER_ENABLE`（框架里根本没有这项）。
这个项目本身也有前科：`USE_SESSION` 曾经「定义了、文档写了、代码从没读过」。

**不能见到未知键就喊**：用户在 `setting.py` 里定义自己的配置给爬虫读是正常用法。
判据是「和某个真配置长得很像」。
"""

from __future__ import annotations

import warnings

import pytest

from mineworker import setting


def _warnings_for(mapping: dict[str, object]) -> list[str]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        setting.apply(mapping)
    return [str(w.message) for w in caught]


@pytest.mark.parametrize(
    ("typo", "meant"),
    [
        ("SPIDER_THREAD_COUNTT", "SPIDER_THREAD_COUNT"),
        ("DOWNLOAD_DELY", "DOWNLOAD_DELAY"),
        ("PROXY_ENABLED", "PROXY_ENABLE"),
        ("ITEM_PIPELINE", "ITEM_PIPELINES"),
        ("USE_SESSIONS", "USE_SESSION"),
        ("MAX_RESPONSE_SIZ", "MAX_RESPONSE_SIZE"),
    ],
)
def test_near_miss_is_called_out(typo: str, meant: str) -> None:
    msgs = _warnings_for({typo: 1})
    assert any(typo in m and meant in m for m in msgs), (
        f"{typo} 被静默接受了 —— 用户会以为配置生效了"
    )


@pytest.mark.parametrize(
    "own",
    ["MY_API_KEY", "SHOP_ID", "TARGET_CITIES", "LOGIN_TOKEN", "KAFKA_BROKERS"],
)
def test_user_defined_settings_are_not_nagged(own: str) -> None:
    """用户自己的配置不能误报 —— 误报比不报更糟，会训练人无视警告。"""
    assert _warnings_for({own: "x"}) == [], f"{own} 被误当成拼错"


@pytest.mark.parametrize(
    "own",
    [
        "PROXY_LIST",  # 松一点就会被说成 PROXY_POOL
        "ITEM_TABLE",  # → ITEM_FILTER_ENABLE
        "SPIDER_NAME",  # → SPIDER_MAX_RUNTIME
        "MAX_PAGES",  # → DEDUP_MAX_LAYERS
        "DOWNLOAD_DIR",  # → DOWNLOAD_DELAY
        "TASK_URL",  # → REDIS_URL
        "LOG_DIR",  # → LOG_FILE
    ],
)
def test_plausible_custom_names_survive_a_looser_threshold(own: str) -> None:
    """**这一组专门钉住阈值**：每个名字在 cutoff 放宽到 0.5 时都会被误报。

    这里有一段值得留下的过程记录。我起初以为上面那组「不误报」用例太弱，
    因为把 cutoff 改成 0.5 之后它们全绿 —— **但那个变异根本没生效**：
    `perl -0pi -e 's/cutoff=0.8/cutoff=0.5/'` 没加 `/g`，只替换了整个文件里的
    第一处，而第一处在**文档字符串**里，真正的代码原封不动。

    正确地改了代码之后，上面那组里 `TARGET_CITIES` / `LOGIN_TOKEN` /
    `KAFKA_BROKERS` 三条确实会红 —— 它们本来就钉得住阈值。

    留这一组是因为它更贴近真实：`PROXY_LIST`、`ITEM_TABLE`、`SPIDER_NAME`、
    `MAX_PAGES` 都是用户很可能在 `setting.py` 里写的自定义配置名，
    而它们全都「离某个真配置只差一点」。
    """
    assert _warnings_for({own: "x"}) == [], (
        f"{own} 被误当成拼错 —— 判据太松，会把用户自己的配置也一起喊，喊多了就没人看警告了"
    )


def test_real_settings_are_silent_and_still_applied() -> None:
    try:
        assert _warnings_for({"SPIDER_THREAD_COUNT": 7}) == []
        assert setting.SPIDER_THREAD_COUNT == 7
    finally:
        setting.reload()


def test_unknown_key_is_still_readable_by_the_spider() -> None:
    """警告归警告，值还是要设进去 —— 用户可能就是靠它传自定义配置。"""
    try:
        setting.apply({"MY_OWN_THING": 42})
        assert setting.MY_OWN_THING == 42  # type: ignore[attr-defined]
    finally:
        setting.reload()


def test_project_setting_file_is_checked_too(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """项目配置文件是最容易写错的地方，必须一起检查。"""
    from pathlib import Path

    path = Path(str(tmp_path)) / "setting.py"
    path.write_text("DOWNLOAD_DELY = 2.0\nMY_API_KEY = 'k'\n", encoding="utf-8")
    monkeypatch.setenv("MINEWORKER_SETTING", str(path))
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            setting.reload()
        msgs = [str(w.message) for w in caught]
        assert any("DOWNLOAD_DELY" in m and "DOWNLOAD_DELAY" in m for m in msgs)
        assert not any("MY_API_KEY" in m for m in msgs), "用户自定义配置被误报"
    finally:
        monkeypatch.delenv("MINEWORKER_SETTING", raising=False)
        setting.reload()
