# 浏览器渲染

需要：

```bash
pip install "mineworker[render]"
playwright install chromium
```

!!! note "停止时排队中的请求会失败，而不是挂住"
    渲染是串行的（`pool_size` 个浏览器轮流处理），停止爬虫时队列里往往还排着请求。
    这些请求会被当场收尾成失败，调用方拿到 `RequestError` ——
    重试用尽后落进 `failed_requests.jsonl`，`mineworker retry --requests` 可回放。

    早先的版本不收尾它们：`submit()` 的等待没有上限，而 `close()` 不排空队列，
    于是那些调用线程**永远醒不来**。实测 1 个在渲染、4 个排队时关闭，
    4 个线程永久挂起 —— `stop()` 它不看、`join()` 它不动，优雅停止就此失效。

## 用法

```python
yield mw.Request(
    "https://spa.example.com/list",
    render=True,
    wait_for="ul.items li",          # 等这个选择器出现再取内容
    render_time=1.0,                 # 之后再等 1 秒
    render_script=lambda page: page.click("button.load-more"),  # 在浏览器线程执行
    callback=self.parse,
)
```

`response.text` 是渲染后的完整 DOM，`.xpath / .css` 照常用。

## 配置

`setting.py`：

```python
WEBDRIVER = dict(
    pool_size=2,          # 并发浏览器数
    browser="chromium",   # chromium | firefox | webkit
    headless=True,
    load_images=False,    # 拦截图片 / 字体 / 媒体，加速
    timeout=30,           # 秒
    wait_until="domcontentloaded",
    proxy=None,
    stealth=True,         # 注入基础反检测脚本
    viewport=[1920, 1080],
)
```

## 架构

`pool_size` 个独立渲染线程，每个持有一个 chromium（Playwright 的 sync API 不能跨线程）。
工作线程把渲染任务投进队列并阻塞等结果 —— 所以 `pool_size` 就是真正的并发浏览器上限，
小于 `SPIDER_THREAD_COUNT` 时天然形成背压。

导航超时 / 连接失败 → `RequestError` → 走正常重试逻辑。爬虫结束时自动关闭所有浏览器。
