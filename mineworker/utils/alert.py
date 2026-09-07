"""告警：卡死 / 失败率 / 失败数 三类检查，多渠道通知（日志 / 飞书 / 邮件）。"""

from __future__ import annotations

import smtplib
import time
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from mineworker import setting
from mineworker.utils import stats as sk
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.utils.stats import Stats

log = get_logger("alert")


class Notifier(Protocol):
    def send(self, title: str, message: str) -> None: ...


class LogNotifier:
    def send(self, title: str, message: str) -> None:
        log.warning("[告警] {}：{}", title, message)


class FeishuNotifier:
    def __init__(self, webhook: str) -> None:
        self._webhook = webhook

    def send(self, title: str, message: str) -> None:
        payload = {"msg_type": "text", "content": {"text": f"【{title}】{message}"}}
        try:
            httpx.post(self._webhook, json=payload, timeout=10)
        except httpx.HTTPError as exc:
            log.error("飞书告警发送失败：{!r}", exc)


class EmailNotifier:
    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config

    def send(self, title: str, message: str) -> None:
        cfg = self._cfg
        to = cfg.get("to") or []
        if not to:
            return
        msg = MIMEText(message, "plain", "utf-8")
        msg["Subject"] = f"[MineWorker] {title}"
        msg["From"] = str(cfg.get("user", ""))
        msg["To"] = ", ".join(to) if isinstance(to, list) else str(to)
        try:
            cls = smtplib.SMTP_SSL if cfg.get("ssl") else smtplib.SMTP
            with cls(str(cfg["host"]), int(cfg.get("port", 25))) as server:
                if cfg.get("user"):
                    server.login(str(cfg["user"]), str(cfg.get("password", "")))
                server.send_message(msg)
        except Exception as exc:  # smtplib 异常种类多
            log.error("邮件告警发送失败：{!r}", exc)


def build_notifiers() -> list[Notifier]:
    notifiers: list[Notifier] = [LogNotifier()]
    if setting.WARNING_FEISHU_WEBHOOK:
        notifiers.append(FeishuNotifier(setting.WARNING_FEISHU_WEBHOOK))
    if setting.WARNING_EMAIL.get("host"):
        notifiers.append(EmailNotifier(setting.WARNING_EMAIL))
    return notifiers


class AlertManager:
    def __init__(
        self,
        stats: Stats,
        notifiers: list[Notifier] | None = None,
        dedup: Any = None,
    ) -> None:
        self._stats = stats
        #: 去重过滤器（可为 None，或没有容量概念的精确去重）—— 只用来读填充度
        self._dedup = dedup
        self._notifiers = notifiers if notifiers is not None else build_notifiers()
        self._last_ok = 0
        self._last_progress = time.monotonic()
        self._last_sent: dict[str, float] = {}

    def check(self) -> None:
        if not setting.WARNING_ENABLE:
            return
        now = time.monotonic()
        data = self._stats.as_dict()
        ok = data.get(sk.REQUEST_OK, 0)
        failed = data.get(sk.REQUEST_FAILED, 0)
        total = ok + failed

        if ok > self._last_ok:
            self._last_ok = ok
            self._last_progress = now

        stall = setting.WARNING_STALL_SECONDS
        if stall and total and now - self._last_progress > stall:
            self._fire("stall", "爬虫疑似卡死", f"{stall:.0f}s 内没有新的成功请求")

        if total >= setting.WARNING_MIN_REQUESTS and failed / total >= setting.WARNING_FAILED_RATE:
            self._fire("failed_rate", "失败率过高", f"失败 {failed} / 总计 {total}")

        if setting.WARNING_FAILED_COUNT and failed >= setting.WARNING_FAILED_COUNT:
            self._fire("failed_count", "失败请求过多", f"已失败 {failed} 个")

        self._check_dedup_fill()

    def _check_dedup_fill(self) -> None:
        """布隆填满会**静默**地把新 URL 当成抓过的丢掉 —— 这是最该有告警的一类失效。

        实测容量 3 倍时每 13 个新 URL 丢 1 个，5 倍时丢一半，而统计里只显示
        「去重 N 条」，看上去完全正常。所以宁可早报。
        """
        rate = setting.DEDUP_WARN_FILL_RATE
        if not rate or self._dedup is None:
            return
        # 精确去重没有容量概念，lite / 自定义过滤器也可能没有 —— 拿不到就跳过
        count = getattr(self._dedup, "count", None)
        capacity = getattr(self._dedup, "capacity", None)
        if not isinstance(count, int) or not isinstance(capacity, int) or capacity <= 0:
            return
        if count < capacity * rate:
            return
        self._fire(
            "dedup_fill",
            "去重过滤器接近容量上限",
            f"已插入 {count:,} / 容量 {capacity:,}。超容后新 URL 会被当成"
            f"「已抓过」静默丢掉（实测 5 倍容量时丢一半）。"
            f"请调大 DEDUP_INITIAL_CAPACITY 或改用精确去重",
        )

    def _fire(self, key: str, title: str, message: str) -> None:
        now = time.monotonic()
        # 「从没发过」必须用 key 缺席表示，不能拿 0.0 当哨兵：time.monotonic() 的原点是开机，
        # 刚启动的机器 / 容器上 now 很小，now - 0.0 < WARNING_INTERVAL 会把第一条告警吞掉。
        last = self._last_sent.get(key)
        if last is not None and now - last < setting.WARNING_INTERVAL:
            return
        self._last_sent[key] = now
        for notifier in self._notifiers:
            try:
                notifier.send(title, message)
            except Exception:
                log.exception("通知渠道 {} 异常", type(notifier).__name__)
