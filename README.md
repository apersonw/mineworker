# MineWorker

一个上手简单、结构清晰的 Python 爬虫框架，对标 [feapder](https://github.com/Boris-code/feapder)：
你只写 `start_requests` 和 `parse`，框架负责调度、下载、重试、去重、批量落库。

> **状态：0.3.0** —— 轻量单机版（AirSpider）完整可用。
> 分布式 / TaskSpider / BatchSpider / 管理平台见 [Roadmap](docs/roadmap.md)。

## 安装

```bash
pip install mineworker            # 核心
pip install "mineworker[all]"     # 含浏览器渲染 / MongoDB / Redis / CLI / 指标
```

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

- 完整文档：`pip install "mineworker[docs]" && mkdocs serve`（见 [docs/](docs/)）
- 设计与实施计划：[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)

## 能力一览

| | |
|---|---|
| 运行时 | 单进程多线程、优先级队列、结束检测、`Ctrl-C` 优雅排空、崩溃 dump + `retry` 回放 |
| 网络 | `Request`/`Response`（httpx + parsel）、自动重试、`validate`/`failed_request` 钩子、随机 UA、会话复用 |
| 数据 | `Item`/`UpdateItem`、`Pipeline`（Console/CSV/MongoDB）、请求级 + Item 级去重（布隆/精确） |
| 渲染 | `Request(render=True)` —— Playwright 渲染池、`wait_for`/`render_time`/`render_script` |
| 扩展 | 下载中间件链、代理池接口 |
| 观测 | Prometheus exporter、卡死/失败率告警（飞书/邮件）、`debug=True` |
| 工具 | `mineworker create/shell/retry` |

## 开发

```bash
conda env create -f environment.yml && conda activate mineworker
pre-commit install
pytest && ruff check . && mypy
```

CI（GitHub Actions）：ruff + mypy(strict) + pytest（Python 3.10–3.13）+ mkdocs build。

## License

[MIT](LICENSE)
