# MineWorker 爬虫框架实施计划

> 工作代号 **MineWorker**（Python 包名待定，示例中用 `mineworker`，导入别名 `mw`）。
> 目标：对标 [feapder](https://github.com/Boris-code/feapder) 的心智模型，先交付一个**轻量单机版（类 AirSpider）**，架构上为后续分布式演进留好接口。
> 语言：Python 3.10+ ｜ 优先存储：Redis（请求队列 + 去重，v2）、MongoDB（落库，v1）
> 预计：单人全职约 8 周到「对外可用」；两人约 5 周。

---

## 1. 定位与设计目标

**要做的**

- 一个「上手简单、结构清晰」的 Python 爬虫框架，用户只写 `start_requests` + `parse`，框架负责调度、下载、重试、去重、批量落库。
- 心智模型与 feapder 一致：`Spider` 类 + `Request` / `Response` / `Item` / `Pipeline`。
- v1 单进程、内存队列，跑通「请求 → 解析 → 落库」闭环，能优雅退出。
- 内置浏览器渲染（Playwright）、内存去重、MongoDB / CSV / Console 管道、CLI 脚手架。

**v1 明确不做（Roadmap v2+）**

- Redis 分布式队列、断点续爬、多节点。
- `TaskSpider` / `BatchSpider`（任务调度、批次采集、任务防丢）。
- 账号 / Cookie 池、代理池的完整实现（只留接口）。
- 管理平台（类 feaplat）的 Web UI。
- MySQL 管道（用户未列为优先项，接口预留）。

**非目标**

- 不追求与 feapder **代码级兼容**（不保证能直接跑 feapder 的爬虫），只追求 **API 心智兼容**。
- 不做通用 async 重写（v1 用线程 + 同步 IO，见 §8 取舍）。

---

## 2. feapder 架构解析

### 2.1 分层与核心模块

| 层 | 目录 | 职责 |
|---|---|---|
| **调度运行时** | `core/scheduler.py` | 编排所有线程；启动 request_buffer / item_buffer / collector / N×parser_control / heartbeat；结束检测（队列空 + 无在途，3×1s 复查）；`check_task_status` 卡死 / 失败率检测 + 告警 |
| | `core/collector.py` | 从任务队列批量拉取任务到内存，喂给 parser_control |
| | `core/parser_control.py` | 工作线程：取 Request → 下载中间件 → 下载 → 校验 → 回调解析 → 分发结果 → 重试 / 失败处理 |
| | `core/base_parser.py` | 用户基类：`start_requests` / `parse` / `download_midware` / `validate` / `failed_request` / 生命周期钩子 |
| **缓冲区** | `buffer/request_buffer.py` | 收集 yield 出的 Request，去重后批量入队；删除已完成、回填失败请求 |
| | `buffer/item_buffer.py` | 收集 Item，按表分组批量交给 Pipeline，单线程写库；成功后写去重指纹，失败 dump 到 `failed_items` |
| **网络** | `network/request.py` | `Request` 封装 requests / session；`fingerprint`（url+body 的 md5）；`to_dict`/`from_dict`（Redis 序列化）；`__lt__` 优先级；代理池集成 |
| | `network/response.py` | `Response` 封装 requests.Response；`.xpath/.css/.re/.json/.bs4`；编码检测；`.urljoin`；渲染页 cookie/page |
| | `network/downloader/` | `base.py` + `_requests.py` / `_selenium.py` / `_playwright.py`，按 Request 选择下载器 |
| | `network/proxy_pool/` `user_pool/` | 代理池；账号池（guest / normal / gold 三档，管理登录态） |
| **去重** | `dedup/` | `Dedup` 门面：`MemoryFilter`（进程内 bloom）/ `BloomFilter`（Redis 可扩展 bloom）/ `ExpireFilter`（Redis 时间窗）/ `LiteFilter`（轻量 set）。`add()` 返回 1 新 / 0 重复 |
| **数据模型** | `network/item.py` | `Item` / `UpdateItem`，元类生成 `__table_name__`（类名下划线化去 `_item`）、`__unique_key__`；`to_sql()`；`fingerprint`；`pre_to_db()` 钩子；按 item 配 `pipelines` |
| **管道** | `pipelines/` | `BasePipeline`：`save_items(table, items)->bool` / `update_items(table, items, update_keys)->bool` / `close()`。实现：console / csv / mysql / mongo |
| **存储适配** | `db/` | `mysqldb`（连接池）/ `redisdb`（集群 / 哨兵）/ `mongodb` / `memorydb`（AirSpider 内存队列） |
| **CLI** | `commands/` | `feapder create` → project / spider / item（读库表反射）/ setting / cookies（curl 转换）/ table；`feapder shell` / `retry` / `zip` |
| **工具** | `utils/` | `log`（loguru）/ `tools`（md5、时间、json、`@retry`、动态导入…）/ `metrics`（InfluxDB）/ `email_sender` / `redis_lock` / `webdriver/`（selenium & playwright 驱动池 + stealth.js） |
| **配置** | `setting.py` | 中心化配置：连接串、线程数、重试次数、缓冲阈值、代理 API、去重、日志、告警（钉钉 / 飞书 / 邮件 / 企微） |

### 2.2 请求生命周期

```
start_requests()                     用户 yield 种子 Request
      │
      ▼
RequestBuffer      去重(filter_repeat) → 按优先级批量入队
      │
      ▼
任务队列            Redis zset（Spider） / 内存 PriorityQueue（AirSpider）
      │
      ▼
Collector          批量 pop 到内存 _todo_requests
      │
      ▼
ParserControl ×N   ① download_midware(request)
 (工作线程)         ② request.get_response()  → 下载器(requests/session/playwright)
                   ③ parser.validate(request, response)   可 raise 触发重试/丢弃
                   ④ 回调 callback / parse  → 生成器 yield Request | Item | callable
                   ⑤ 分发：Request→RequestBuffer  Item→ItemBuffer  callable→队列
                   ⑥ 异常/坏响应：retry_times++；< MAX 则回队（关去重），
                      否则 failed_request() 钩子 + dump 到 failed_requests
      │
      ▼
ItemBuffer         按表批量 → pipeline.save_items()
                   成功 → Dedup.add(fingerprint)；失败 → dump failed_items
      │
      ▼
Scheduler          heartbeat + check_task_status；
                   队列空 ∧ 无在途，连续 3 次(1s 间隔) → end_callback() → 停止
```

### 2.3 四种 Spider 的差异

| Spider | 队列 / 去重 | 适用 | v1 是否做 |
|---|---|---|---|
| **AirSpider** | 内存 Queue + 内存去重，无 Redis | 数据量小、一次性、轻量 | ✅ v1 目标 |
| **Spider** | Redis zset + Redis 去重，断点续爬，多节点 | 海量数据、分布式、需续爬 | ⏳ v2 |
| **TaskSpider** | 周期性从 Redis/DB 拉种子任务，分布式 | 持续消费任务表 | ⏳ v2 |
| **BatchSpider** | MySQL 任务表 + 批次记录表，批次进度 / 监控，定时 | 周期性全量 / 增量批次采集 | ⏳ v3 |

---

## 3. MineWorker v1 架构设计

### 3.1 运行时数据流（单进程 / 线程）

```
start_requests() ──► RequestBuffer ──► MemoryTaskQueue (PriorityQueue)
                        │  去重                    │
                        │  (Dedup)          Collector  批量 pop → 内存 deque
                        │                          │
                        ▼            ┌─────────────┼─────────────┐
                  (回填失败/删除)   ParserWorker  ParserWorker  ParserWorker   线程×N
                                        │  download_midware
                                        │  → Downloader(httpx / playwright) → Response
                                        │  → validate → callback(parse)
                                        │  → yield Request | Item
                          ┌─────────────┴─────────────┐
                    RequestBuffer                ItemBuffer   批量 flush
                                                      │  → Pipelines: Console / CSV / Mongo
                                                      │  成功 → Dedup.add(item.fingerprint)
                                                      ▼
                                                 failed_items.jsonl (本地 dump)

AirScheduler：启动全部线程 · 运行 start_requests · 结束检测 · SIGINT 优雅排空 · 统计汇总
```

### 3.2 包结构（提案）

```
mineworker/
├── __init__.py            # 导出 AirSpider, Request, Response, Item, UpdateItem
├── setting.py             # 框架默认配置
├── exceptions.py
├── core/
│   ├── scheduler.py       # AirScheduler：线程编排 + 结束检测
│   ├── base_parser.py     # BaseParser：start_requests / parse / validate / 钩子
│   ├── collector.py       # MemoryCollector
│   ├── parser_control.py  # ParserWorker（线程）
│   └── spiders/
│       └── air_spider.py  # AirSpider = AirScheduler + BaseParser
├── buffer/
│   ├── request_buffer.py  # 去重 + 入队 + 失败回填
│   └── item_buffer.py     # 批量 + 管道分发 + 去重标记
├── network/
│   ├── request.py         # Request（httpx 封装）
│   ├── response.py        # Response（parsel 封装）
│   ├── selector.py
│   ├── user_agent.py
│   └── downloader/
│       ├── base.py        # Downloader ABC
│       ├── _httpx.py      # 默认
│       └── _playwright.py # render=True
├── dedup/
│   ├── __init__.py        # Dedup 门面
│   ├── memory_filter.py   # bitarray 可扩展 bloom
│   └── lite_filter.py     # set 精确去重
│   # redis_filter.py      → v2
├── db/
│   ├── mongodb.py         # pymongo 封装
│   └── redisdb.py         # 接口 + 配置就绪，v2 启用
├── pipelines/
│   ├── base.py            # BasePipeline ABC
│   ├── console.py
│   ├── csv.py
│   └── mongo.py
├── utils/
│   ├── log.py             # loguru 封装
│   ├── tools.py           # md5 / fingerprint / 时间 / json / @retry / 动态导入
│   └── metrics.py         # 默认 no-op，可切 prometheus
├── commands/
│   ├── cmdline.py         # `mineworker create / shell / retry`
│   └── create/            # 各子命令
└── templates/
    ├── air_spider.tmpl
    ├── item.tmpl
    └── project/           # main.py / setting.py / spiders/ / items/
```

---

## 4. 技术选型

| 关注点 | 选型 | 说明 / 与 feapder 差异 |
|---|---|---|
| HTTP 客户端 | **httpx**（同步 + 连接池） | feapder 用 requests；httpx 超时模型清晰、支持 HTTP/2、未来可平滑切 async |
| HTML 解析 | **parsel**（Scrapy 同款 `Selector`，xpath/css/re） | 高性能场景补 `selectolax` |
| 浏览器渲染 | **playwright**（sync API） | feapder 支持 selenium + playwright，我们只做 playwright |
| 去重 | **bitarray** 自实现可扩展 bloom + `hashlib`；`LiteFilter` 精确 set | 备选 `pybloom-live` |
| 日志 | **loguru** | 与 feapder 一致，按 spider 分文件 |
| MongoDB | **pymongo**（`bulk_write` / upsert） | |
| Redis（v2） | **redis-py 5.x** | v1 只写接口和配置 |
| CLI | **typer** + **jinja2** | feapder 用 argparse |
| 配置 | 分层：框架默认 ← 项目 `setting.py` ← 环境变量；可选 `pydantic-settings` | |
| 打包 | **uv** + `pyproject.toml`(PEP 621)；extras：`[render]` `[mongo]` `[redis]` `[all]` | feapder 也用 extras 分层安装 |
| 测试 | **pytest** + `respx` / `pytest-httpserver` | |
| 代码质量 | **ruff** + **mypy** + **pre-commit** | |
| 文档 | **mkdocs-material** | |

---

## 5. 分阶段实施计划

> 每阶段给出**目标 / 涉及模块 / 验收(DoD) / 预计工期（单人）**。阶段间强依赖，按序推进。

### 阶段 00 · 地基 ｜ ~3–5 天

- **目标**：仓库骨架 + 工具层 + 配置系统。
- **模块**：`pyproject.toml`（uv / PEP 621）、CI（GitHub Actions：ruff + mypy + pytest）、pre-commit、LICENSE；
  `utils/log.py`（loguru 封装）、`utils/tools.py`（`md5` / `get_fingerprint` / 时间格式化 / `dumps_json` / `@retry` 装饰器 / `load_object` 动态导入）、
  `setting.py` 分层加载（框架默认 ← 项目 `setting.py` ← `MINEWORKER_*` 环境变量）、`exceptions.py`。
- **DoD**：`import mineworker` 通过；`pytest` 冒烟测试绿；CI lint / type 全绿。

### 阶段 01 · 网络层（Request / Response / Downloader）｜ ~1 周

- **目标**：能发请求、拿到增强版 Response、抽取数据、跟进链接。
- **模块**：
  - `Request`：构造参数对齐 feapder 子集（`url` `method` `callback`(str/fn) `priority=300` `retry_times` `filter_repeat=True` `auto_request=True` `use_session` `random_user_agent` `render` `render_time` `download_midware` `cb_kwargs` + `**requests_kwargs`）；`fingerprint`（`method+url+sorted(params/data)` 的 md5）；`__lt__`；`to_dict`/`from_dict`（JSON 安全，v2 Redis + 失败 dump 复用）；`copy()`。
  - `Downloader` ABC + `HttpxDownloader`：连接池、可选 per-request session、超时 / 重试 / verify、代理钩子、随机 UA。
  - `Response`：包 `httpx.Response`；`.text`（`charset-normalizer` 检测）`.json` `.xpath/.css/.re`（parsel）`.bs4`（可选）`.urljoin` `.status_code` `.request` `.to_dict`。
  - `validate(request, response)` 校验钩子（raise → 重试 / 丢弃）。
- **DoD**：抓取一个真实页面 → `Response.xpath()` 正确 → 跟进链接；校验 / 重试路径有单测（`respx` mock）。

### 阶段 02 · 核心运行时（Scheduler / Buffer / Collector / Worker）｜ ~1.5 周 ★关键里程碑

- **目标**：AirSpider 跑通完整闭环并能优雅退出。**首个可用版本 0.1.0**。
- **模块**：
  - `MemoryTaskQueue`：`queue.PriorityQueue`，key = `(priority, 单调序号)`。
  - `RequestBuffer`：接收 yield 的 Request，`filter_repeat` 时经 `Dedup` 去重，批量入队；`put_failed_request()`（关去重回队）、`delete_done()`。
  - `MemoryCollector`：批量 pop `COLLECTOR_TASK_COUNT` 到内部缓冲；`get_request()` 供 worker 取；空信号。
  - `ParserWorker`（线程）：`get Request → download_midware → get_response → validate → 解析回调（parse / callback，支持 parser_name 跨类）→ 迭代结果分类 → 入 buffer → 异常时 retry / failed_request`；每请求统计。
  - `AirScheduler`：启动 N×worker + request_buffer flush 线程 + item_buffer flush 线程 + collector；跑 `start_requests`；**结束检测**：队列空 ∧ collector 空 ∧ 所有 worker idle（原子计数器）∧ buffer 空，连续 3 次(1s) → `end_callback` → join 停止；`SIGINT` 优雅排空；生命周期钩子 `start_callback` / `end_callback`。
  - 统计汇总器：请求成功 / 失败、item 数、重试数、运行时长 → 结束打印。
- **DoD**：demo 爬虫爬完一个分页测试站并干净退出；`Ctrl-C` 能优雅排空；无僵尸线程。

### 阶段 03 · 数据层（Item / Pipeline / Dedup / Mongo）｜ ~1 周

- **目标**：结构化落库 + 去重。
- **模块**：
  - `Item` + 元类：`__table_name__`（类名 snake_case 去 `_item`）、`__unique_key__`；动态字段（`__init__` / 属性赋值）；`to_dict` `fingerprint` `pre_to_db()`；按 item 配 `pipelines`。`UpdateItem` + `__update_key__`。
  - `ItemBuffer`：按 `ITEM_MAX_CACHED_COUNT` / flush 间隔批量；按表分组；调 `save_items` / `update_items`；成功 `Dedup.add(fingerprint)`；失败 → `failed_items.jsonl`；单写线程。
  - `BasePipeline` ABC；`ConsolePipeline` / `CsvPipeline` / `MongoPipeline`（pymongo，`UpdateItem` 按 unique_key upsert，`bulk_write`）。
  - `Dedup` 门面：`MemoryBloomFilter`（bitarray，可配 capacity / error_rate）+ `LiteFilter`（set）；`add` / `get`；`to_md5`。Redis 版留接口。
  - 请求级去重 + item 级去重双通路接好。
- **DoD**：demo 爬虫把去重后的文档写入真实 MongoDB；重跑无新增；CSV / Console 管道验证通过。

### 阶段 04 · 浏览器渲染（`render=True`）｜ ~4–6 天

- **目标**：JS 渲染页也能解析。
- **模块**：
  - `PlaywrightDownloader`：驱动池（复用 browser context；`WEBDRIVER` 配置：headless / proxy / UA / 是否加载图片 / 超时 / `render_time` / stealth JS 注入）。sync API 配合线程 worker；或独立 render worker 线程池。
  - 渲染版 `Response`：`.text` = `page.content()`、cookies、截图、`.page` 访问、`page.wait_for_selector`。
  - worker / scheduler 停止时回收浏览器资源。
- **DoD**：一个 JS 渲染 demo 页产出正确数据；池能限制并发浏览器数。

### 阶段 05 · CLI 与脚手架（`mineworker` 命令）｜ ~4–5 天

- **目标**：`mineworker create -p demo && python demo/main.py` 直接跑通。
- **模块**：
  - `typer` CLI：`create -p <project>` / `create -s <SpiderName> [--type air]` / `create -i <ItemName>` / `create --setting` / `shell <url>`（IPython 注入 `request` / `response`）/ `retry`（回放 `failed_requests` / `failed_items` dump）。
  - jinja2 模板：project / air_spider / item / setting / main.py。
- **DoD**：脚手架生成的项目开箱即跑；`mineworker shell` 可交互调试。

### 阶段 06 · 可观测性与健壮性 ｜ ~1 周

- **目标**：能看指标、会告警、可恢复；文档上线。
- **模块**：
  - 统计 + 可选 Prometheus exporter（`prometheus-client`）：请求速率、队列深度、在途数、item 数、失败率；`metrics.py` 默认 no-op。
  - 告警钩子接口（`WARNING_*`）：卡死检测（N 秒无进展）、失败率阈值 → `send_msg` 适配器（**飞书 webhook 优先**，其次邮件；钉钉 / 企微后续）。
  - `download_midware` 链 + 全局中间件列表 + 每爬虫 `__custom_setting__`。
  - 代理池接口（`ProxyPool` ABC + API 抽取实现）+ `random_user_agent` UA 池。
  - 错误分类、`--debug` 模式、从 dump 恢复。
  - mkdocs-material 文档站：快速开始 / 概念 / API。
- **DoD**：dashboard 显示实时指标；模拟失败触发飞书告警；文档发布。

---

## 6. feapder → MineWorker 模块对照

| feapder | MineWorker v1 | 备注 |
|---|---|---|
| `core/scheduler.py` | `core/scheduler.py`（`AirScheduler`） | 单进程线程编排 + 结束检测 |
| `core/collector.py` | `core/collector.py`（`MemoryCollector`） | v2 换 Redis |
| `core/parser_control.py` | `core/parser_control.py`（`ParserWorker`） | |
| `core/base_parser.py` | `core/base_parser.py` | |
| `buffer/request_buffer.py` | `buffer/request_buffer.py` | 去重 + 入队 |
| `buffer/item_buffer.py` | `buffer/item_buffer.py` | 批量 + 管道分发 |
| `network/request.py`（requests） | `network/request.py`（httpx） | |
| `network/response.py` | `network/response.py`（parsel） | |
| `network/downloader/_requests.py` | `network/downloader/_httpx.py` | |
| `network/downloader/_playwright.py` | 同名 | 阶段 04 |
| `network/downloader/_selenium.py` | — | 不做，Playwright 足够 |
| `network/proxy_pool/` | 接口保留 | 阶段 06 简版 |
| `network/user_pool/` | — | v2 |
| `dedup/`（含 Redis bloom） | `dedup/`（memory bloom + lite） | Redis 版 v2 |
| `db/mongodb.py` | `db/mongodb.py` | |
| `db/redisdb.py` | 接口 + 配置就绪 | v2 启用 |
| `db/mysqldb.py` `db/memorydb.py` | — / 内存队列并入 core | |
| `pipelines/mongo_pipeline.py` | `pipelines/mongo.py` | |
| `pipelines/{console,csv}` | 同名 | |
| `pipelines/mysql_pipeline.py` | — | v2 |
| `commands/`（argparse） | `commands/`（typer） | |
| `utils/log.py`（loguru） | `utils/log.py` | |
| `utils/metrics.py`（InfluxDB） | `utils/metrics.py`（Prometheus） | |
| `utils/webdriver/` | 并入 `network/downloader/_playwright.py` | |
| `core/spiders/{spider,task_spider,batch_spider}` | — | Roadmap v2 / v3 |

---

## 7. 里程碑

| 里程碑 | 时点（单人） | 交付 |
|---|---|---|
| **M1** | 第 2 周末 | 阶段 00–01：能发请求、解析、跟进链接 |
| **M2** | 第 4 周末 | 阶段 02：AirSpider 闭环 + 优雅退出。**0.1.0** |
| **M3** | 第 6 周末 | 阶段 03–04：Mongo 落库 + 去重 + 浏览器渲染。**0.2.0** |
| **M4** | 第 8 周末 | 阶段 05–06：CLI + 指标 / 告警 + 文档。**0.3.0（对外可用）** |

两人并行：一人网络层 + 渲染，一人运行时 + 数据层，可压缩到约 5 周。

---

## 8. 风险与取舍

| 主题 | 决策 | 缓解 |
|---|---|---|
| **同步 vs 异步** | v1 用线程 + 同步 httpx（心智与 feapder 一致、Playwright sync 好配合、调试简单） | `Downloader` 抽象隔离；`Response` 不泄漏 httpx 类型，v2 可评估 async |
| **结束检测竞态** | 沿用 feapder 的 3×1s 复查 | 增加「worker 在途」原子计数器，避免 buffer 刚空但 worker 仍在解析时误判结束 |
| **内存队列无断点续爬** | v1 明确不做 | 崩溃 / 退出时 dump 未完成 Request 到 JSONL，`mineworker retry` 恢复 |
| **Bloom 误判** | 默认 `error_rate=1e-6` | 关键场景切 `LiteFilter`（精确 set，内存换准确） |
| **Playwright 线程模型** | sync API 的 page 不能跨线程共享 | 每个 render worker 独占 context，或单独 render 线程池 |
| **与 feapder 兼容度** | 只做「心智兼容」不做「代码兼容」 | 文档明确说明差异；提供迁移对照 |
| **包名 / 生态位** | `mineworker` 待 PyPI 确认 | 早定名，`__init__` 统一导出面 |

---

## 9. Roadmap v2+（架构已预留接口）

1. **`Spider`（分布式）**：`MemoryTaskQueue`→Redis zset；`MemoryCollector`→Redis 批量 pop；`Dedup`→Redis 可扩展 bloom；断点续爬；`start_requests` 一次性锁（`redis_lock`）；多节点 heartbeat；`failed_requests` 落 Redis。v1 的 buffer / collector / dedup 接口按此设计。
2. **`TaskSpider`**：周期性从 Redis / Mongo / MySQL 拉种子任务。
3. **`BatchSpider`**：批次记录表、进度追踪、定时批次、MySQL 任务表、任务防丢。
4. **账号 / Cookie 池**（guest / normal / gold）、登录态管理。
5. **代理池**完整实现（抽取 / 校验 / 轮换 / 拉黑）。
6. **管理平台（类 feaplat）**：部署 / 调度 / 日志 / 报警 Web UI，独立服务。
7. **MySQL 管道** + `mineworker create -i` 读库表反射生成 Item。

---

## 10. 附录：用户侧 API 示例（目标形态）

```python
import mineworker as mw


class NewsSpider(mw.AirSpider):
    __custom_setting__ = dict(
        SPIDER_THREAD_COUNT=8,
        ITEM_MAX_CACHED_COUNT=200,
        REQUEST_TIMEOUT=15,
    )

    def start_requests(self):
        for page in range(1, 11):
            yield mw.Request(
                f"https://example.com/news?p={page}",
                callback=self.parse_list,
            )

    def parse_list(self, request, response):
        for a in response.xpath('//a[@class="title"]'):
            yield mw.Request(
                a.xpath("./@href").extract_first(),
                callback=self.parse_detail,
                cb_kwargs={"list_url": request.url},
            )

    def parse_detail(self, request, response, list_url=None):
        item = mw.Item()
        item.table_name = "news"
        item.title = response.xpath("//h1/text()").extract_first()
        item.url = request.url
        item.from_list = list_url
        yield item

    def failed_request(self, request, response):
        # 重试耗尽后的兜底：可再 yield Request 或 Item
        yield request


if __name__ == "__main__":
    NewsSpider().start()
```

```python
# setting.py（项目级，覆盖框架默认）
SPIDER_THREAD_COUNT = 4
COLLECTOR_TASK_COUNT = 100
SPIDER_MAX_RETRY_TIMES = 3
ITEM_MAX_CACHED_COUNT = 500
ITEM_UPLOAD_INTERVAL = 1  # 秒

DEDUP_FILTER = "memory"  # memory | lite
DEDUP_ERROR_RATE = 1e-6

MONGO_URI = "mongodb://localhost:27017"
MONGO_DB = "mineworker"

ITEM_PIPELINES = [
    "mineworker.pipelines.console.ConsolePipeline",
    "mineworker.pipelines.mongo.MongoPipeline",
]

WEBDRIVER = dict(
    pool_size=2,
    headless=True,
    load_images=False,
    timeout=30,
    render_time=0,
)

WARNING_FEISHU_WEBHOOK = ""  # 空则不告警
WARNING_FAILED_RATE = 0.5  # 失败率阈值
```
