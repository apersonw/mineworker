# MineWorker

[![PyPI](https://img.shields.io/pypi/v/mineworker)](https://pypi.org/project/mineworker/)
[![Python](https://img.shields.io/pypi/pyversions/mineworker)](https://pypi.org/project/mineworker/)
[![CI](https://github.com/apersonw/mineworker/actions/workflows/ci.yml/badge.svg)](https://github.com/apersonw/mineworker/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-apersonw.github.io-teal)](https://apersonw.github.io/mineworker/)

一个上手简单、结构清晰的 Python 爬虫框架，对标 [feapder](https://github.com/Boris-code/feapder)：
你只写 `start_requests` 和 `parse`，框架负责调度、下载、重试、去重、批量落库。

> **0.5.0** —— 单机（`AirSpider`）到分布式（`Spider` / `TaskSpider` / `BatchSpider`）全部可用，
> 并支持[浏览器 TLS 指纹伪装](https://apersonw.github.io/mineworker/anti-bot/)。
> 变更见 [CHANGELOG](CHANGELOG.md)，后续规划见 [Roadmap](https://apersonw.github.io/mineworker/roadmap/)。

## 安装

```bash
pip install mineworker            # 核心
pip install "mineworker[all]"     # 含渲染 / MongoDB / MySQL / Redis / CLI / 指标
```

也可以按需装单项：`render` · `mongo` · `mysql` · `redis` · `cli` · `metrics`。

## 快速开始

```bash
pip install "mineworker[cli]"
mineworker create -p news_crawler
cd news_crawler && python main.py
```

或直接写：

```python
import mineworker as mw


class NewsSpider(mw.AirSpider):
    def start_requests(self):
        yield mw.Request("https://news.ycombinator.com/", callback=self.parse)

    def parse(self, request, response):
        for a in response.css("span.titleline > a"):
            yield {"title": a.css("::text").get(), "url": a.css("::attr(href)").get()}


NewsSpider().start()
```

## 文档

完整文档：**<https://apersonw.github.io/mineworker/>**
（本地预览：`pip install "mineworker[docs]" && mkdocs serve`）

设计与实施计划见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

## 能力一览

| | |
|---|---|
| 运行时 | 单进程多线程、优先级队列、结束检测、`Ctrl-C` 优雅排空、崩溃 dump + `retry` 回放 |
| 网络 | `Request`/`Response`（httpx + parsel）、自动重试、`validate`/`failed_request` 钩子、随机 UA、会话复用 |
| 分布式 | `Spider`：Redis 队列 + 布隆去重 + 断点续爬 + 种子一次性锁 + 多节点心跳结束检测 |
| 任务驱动 | `TaskSpider` 从任务源持续消费；`BatchSpider` 周期批次采集（任务表状态机 + 进度追踪 + 防丢） |
| 数据 | `Item`/`UpdateItem`、`Pipeline`（Console/CSV/MongoDB/MySQL upsert）、请求级 + Item 级去重（布隆/精确） |
| 渲染 | `Request(render=True)` —— Playwright 渲染池、`wait_for`/`render_time`/`render_script` |
| 反爬 | TLS / HTTP2 指纹伪装（`impersonate` 真实浏览器）、Cloudflare / Akamai 挑战页识别 |
| 扩展 | 下载中间件链、代理池接口、账号 / Cookie 池（掉登录自动换号） |
| 观测 | Prometheus exporter、卡死/失败率告警（飞书/邮件）、`debug=True` |
| 工具 | `mineworker create/shell/retry`，`create -i --table` 读表结构反射生成 Item |

## 开发

```bash
conda env create -f environment.yml && conda activate mineworker
pre-commit install
pytest && ruff check . && mypy
```

CI（GitHub Actions）：ruff + mypy(strict) + pytest（Python 3.10–3.13）+ mkdocs build。

## License

[MIT](LICENSE)
