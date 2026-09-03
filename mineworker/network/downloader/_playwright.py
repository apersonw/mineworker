"""基于 Playwright 的浏览器渲染下载器。

架构：``pool_size`` 个独立渲染线程，每个线程持有一个 chromium（Playwright 的 sync API
不能跨线程用）。工作线程把渲染任务丢进队列并阻塞等结果，因此 ``pool_size`` 就是真正的
并发浏览器上限；小于 ``SPIDER_THREAD_COUNT`` 时天然形成背压。

需要 ``pip install mineworker[render] && playwright install chromium``。
"""

from __future__ import annotations

import queue
import threading
from typing import TYPE_CHECKING, Any

from mineworker import setting
from mineworker.exceptions import RequestError
from mineworker.network.downloader.base import Downloader
from mineworker.network.response import Response
from mineworker.network.user_agent import get_random_user_agent
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.network.request import Request

log = get_logger("downloader.playwright")

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
"""
_BLOCKED_RESOURCES = frozenset({"image", "media", "font"})


class _Job:
    __slots__ = ("error", "event", "request", "response")

    def __init__(self, request: Request) -> None:
        self.request = request
        self.event = threading.Event()
        self.response: Response | None = None
        self.error: BaseException | None = None


class _RenderWorker(threading.Thread):
    def __init__(self, index: int, jobs: queue.Queue[_Job | None], config: dict[str, Any]) -> None:
        super().__init__(name=f"render-{index}", daemon=True)
        self._jobs = jobs
        self._config = config
        self._stop_event = threading.Event()
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    job = self._jobs.get(timeout=0.3)
                except queue.Empty:
                    continue
                if job is None:
                    break
                try:
                    self._ensure_browser()
                    job.response = self._render(job.request)
                except Exception as exc:
                    job.error = exc
                finally:
                    job.event.set()
        finally:
            self._teardown()

    def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        from playwright.sync_api import sync_playwright

        cfg = self._config
        self._pw = sync_playwright().start()
        browser_type = getattr(self._pw, cfg.get("browser", "chromium"))
        launch_kwargs: dict[str, Any] = {"headless": cfg["headless"]}
        if cfg.get("proxy"):
            launch_kwargs["proxy"] = {"server": cfg["proxy"]}
        self._browser = browser_type.launch(**launch_kwargs)

        ua = cfg.get("user_agent")
        if not ua and setting.RANDOM_USER_AGENT:
            ua = get_random_user_agent()
        ctx_kwargs: dict[str, Any] = {}
        if ua:
            ctx_kwargs["user_agent"] = ua
        viewport = cfg.get("viewport")
        if viewport:
            ctx_kwargs["viewport"] = {"width": viewport[0], "height": viewport[1]}
        self._context = self._browser.new_context(**ctx_kwargs)

        if not cfg.get("load_images", False):
            self._context.route("**/*", _maybe_block)
        if cfg.get("stealth", True):
            self._context.add_init_script(_STEALTH_JS)
        log.debug("渲染线程 {} 已启动 {}", self.name, cfg.get("browser", "chromium"))

    def _render(self, request: Request) -> Response:
        cfg = self._config
        timeout_ms = float(cfg["timeout"]) * 1000
        page = self._context.new_page()
        try:
            nav = page.goto(
                request.url, timeout=timeout_ms, wait_until=cfg.get("wait_until", "load")
            )
            wait_for = request.wait_for or cfg.get("wait_for")
            if wait_for:
                page.wait_for_selector(wait_for, timeout=timeout_ms)
            render_time = (
                request.render_time
                if request.render_time is not None
                else cfg.get("render_time", 0)
            )
            if render_time:
                page.wait_for_timeout(float(render_time) * 1000)
            if callable(request.render_script):
                request.render_script(page)

            html = page.content()
            cookies = {c["name"]: c["value"] for c in self._context.cookies()}
            status = nav.status if nav is not None else 200
            headers = dict(nav.headers) if nav is not None else {}
            headers.setdefault("content-type", "text/html; charset=utf-8")
            return Response(
                url=page.url,
                status_code=status,
                content=html.encode("utf-8"),
                headers=headers,
                cookies=cookies,
                request=request,
                encoding="utf-8",
            )
        finally:
            page.close()

    def _teardown(self) -> None:
        for obj, method in (
            (self._context, "close"),
            (self._browser, "close"),
            (self._pw, "stop"),
        ):
            if obj is None:
                continue
            try:
                getattr(obj, method)()
            except Exception:
                log.debug("渲染资源清理异常", exc_info=True)
        self._context = self._browser = self._pw = None


def _maybe_block(route: Any) -> None:
    try:
        if route.request.resource_type in _BLOCKED_RESOURCES:
            route.abort()
        else:
            route.continue_()
    except Exception:  # 页面已关闭等
        pass


class _RenderPool:
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._workers: list[_RenderWorker] = []
        self._lock = threading.Lock()
        self._started = False

    def _ensure_started(self) -> None:
        with self._lock:
            if self._started:
                return
            size = max(1, int(self._config.get("pool_size", 1)))
            for i in range(size):
                worker = _RenderWorker(i, self._jobs, self._config)
                worker.start()
                self._workers.append(worker)
            self._started = True
            log.info("渲染池启动，{} 个浏览器", size)

    def submit(self, request: Request) -> Response:
        self._ensure_started()
        job = _Job(request)
        self._jobs.put(job)
        job.event.wait()
        if job.error is not None:
            raise RequestError(f"渲染失败 {request.url}：{job.error!r}") from job.error
        assert job.response is not None
        return job.response

    def close(self) -> None:
        with self._lock:
            if not self._started:
                return
            for worker in self._workers:
                worker.stop()
            for _ in self._workers:
                self._jobs.put(None)
            for worker in self._workers:
                worker.join(timeout=10)
            self._workers.clear()
            self._started = False


class PlaywrightDownloader(Downloader):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = {**setting.WEBDRIVER, **(config or {})}
        self._pool: _RenderPool | None = None
        self._lock = threading.Lock()

    def download(self, request: Request) -> Response:
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    self._pool = _RenderPool(self._config)
        return self._pool.submit(request)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None
