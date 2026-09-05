# Changelog

本文件格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- PostgreSQL / Elasticsearch / Kafka 管道；抽出 `SqlPipeline` 基类
- `benchmarks/` 吞吐画像套件

### 性能

- **默认配置下吞吐提升约 3.2×**（32 线程 97 → 308 QPS）：缓存 `SSLContext`。
  `httpx.Client()` 每次构造要新建 SSL context（实测 32.9ms），而下载器默认每请求
  新建一个 Client —— 这是框架最大的单项开销。缓存后降到 0.4ms。
  **抓取语义零变化**（cookie 仍每请求隔离）

### 修复

- `setting.USE_SESSION` 是死配置：定义了、文档写了，但框架代码从没读过它，
  只有 `Request(use_session=)` 生效。现在两者都生效（请求级优先）

## [0.5.0] - 2026-09-04

反爬对抗：从 TLS 握手层解决问题，而不是继续换 User-Agent。

### 新增

- **TLS / HTTP2 指纹伪装** —— 新下载器 `CurlDownloader`（基于
  [curl_cffi](https://github.com/lexiforest/curl_cffi) / libcurl-impersonate）。
  设 `DOWNLOADER_IMPERSONATE = "chrome"` 即启用，**爬虫代码零改动**；
  也可按请求覆盖：`Request(url, impersonate="safari17_0")`。
  需 `pip install "mineworker[curl]"`（已并入 `all`）
- **反爬拦截识别** —— 自动识别 Cloudflare / Akamai 挑战页与 JS 跳转空壳，
  抛 `AntiBotError`。它继承 `RequestError`，因此直接复用既有的重试路径，
  重试时代理池会换一个出口 IP。开关 `ANTIBOT_DETECT`（默认开）
- 新文档：[反爬对抗](https://apersonw.github.io/mineworker/anti-bot/)

### 变更

- **启用 `impersonate` 时不再注入随机 User-Agent**。`impersonate` 自带一整套自洽的
  浏览器头，再叠加 UA 池会造成「TLS 握手说 Chrome、UA 头说 Firefox」的自相矛盾 ——
  这比不伪装更容易被识破。显式传入的 `headers` 仍然优先
- 下载器选择顺序：`render` > `impersonate` > `DOWNLOADER_ASYNC` > `use_session` > 默认
- `Request.impersonate` 参与序列化，分布式模式下经 Redis 传递不会丢失

### 说明

为什么换 UA 不够：现代反爬看的是 TLS 握手指纹（JA3/JA4）和 HTTP/2 SETTINGS 帧 ——
在你发出第一个字节之前就已经暴露。实测同一端点，httpx 是
`JA4 t13d1712h1_…` + HTTP/1.1 而 UA 却自称 Firefox 126（三重矛盾），
伪装后为 `JA4 t13d1516h2_…` + h2 + Chrome UA，三者自洽。

## [0.4.0] - 2026-09-04

**首次发行到 PyPI。** 0.3.0 之后积累的全部分布式能力（v2.1–v2.7）随本版本一次性发出。

### 新增

- **Redis 基础设施** —— `db/redisdb.py`（连接缓存、`acquire_once` 一次性锁、统一 key 前缀）、
  `RedisTaskQueue`（zset 优先级队列，原子 `zpopmin`）、`RedisSetFilter` / `RedisBloomFilter`；
  `DEDUP_FILTER=redis|redis-set` 即可整体切换去重后端
- **`Spider`（分布式）** —— Redis 队列 + Redis 布隆去重 + 断点续爬 + `start_requests` 一次性锁 +
  多节点心跳结束检测 + 失败请求落 Redis。抽出 `BaseScheduler` 公共骨架
- **`TaskSpider`** —— 从 Redis / DB 任务源持续拉任务，多节点分摊，`keep_alive` 常驻
- **账号 / Cookie 池** —— `LocalUserPool` / `GuestUserPool` / `RedisUserPool`；
  `user_pool()` + `check_login()` 钩子，掉登录自动换号重试
- **MySQL 管道** —— `MysqlPipeline`（`executemany` 批量写 + `ON DUPLICATE KEY UPDATE` upsert）、
  `MysqlDB`（PooledDB 连接池）；`mineworker create -i --table <表>` 读 `SHOW FULL COLUMNS` 反射生成 Item
- **`AsyncHttpxDownloader`** —— 可选异步下载器（`DOWNLOADER_ASYNC=True`），独立事件循环线程 +
  共享 `AsyncClient`，爬虫代码零改动；新增 `HTTPX_HTTP2` 开关
- **`BatchSpider`** —— 周期性批次采集：MySQL 任务表状态机 + 批次记录表 + 进度追踪 + 任务防丢；
  master（`start_monitor`）/ worker（`start`）分离，抽象 `BatchStore`（`MysqlBatchStore` / `MemoryBatchStore`）
- 文档站新增《分布式》《批次采集》《账号 / Cookie 池》《async 内核评估》四篇

### 修复

- `cb_kwargs` 从未传给 callback（`parser_control` 现在按 `callback(request, response, **request.cb_kwargs)` 调用）
- `BaseScheduler.run()` 中 `_seed()` 移到 `_start_threads()` 之前，避免 worker 抢跑断点续爬遗留的队列
- **刚启动的机器 / 容器上第一条告警被静默吞掉**：`AlertManager` 用 `0.0` 当「从没发过」的哨兵，
  而 `time.monotonic()` 的原点是开机，`now - 0.0 < WARNING_INTERVAL` 在低 uptime 时恒真。
  现在用「key 缺席」表示从没发过
- **同源问题让代理池在新容器里起不来**：`ApiProxyPool` 的 `_last_fetch` 初值 `0.0` 会让第一次
  拉取代理被间隔限流跳过，池子一直是空的。现在初值为 `None`
- 只装核心包（不带 `[cli]`）时执行 `mineworker` 会抛 `ModuleNotFoundError` 堆栈，
  现在提示 `pip install "mineworker[cli]"`

### 变更

- 包元数据：`Development Status` 提到 `4 - Beta`，补 `Typing :: Typed` 与完整的 `[project.urls]`
- 新增 PyPI 发布与文档部署流水线（Trusted Publishing，无 token）

### 说明

- 关于「是否把内核改成全 async」的评估结论见
  [async 内核评估](https://apersonw.github.io/mineworker/async-kernel/)：**不做全量重写**，
  只落地隔离的异步下载器。工作线程仍是「1 线程 1 在途」，`AsyncHttpxDownloader` 的收益是
  连接复用 / HTTP2 / 更低 FD

## 0.3.0 - 2026-09-03

轻量单机版（`AirSpider`）完整可用。**未发行到 PyPI**（当时仅本地开发）。

### 新增

- **运行时** —— 内存优先级队列、`Collector` / `ParserControl` / `RequestBuffer` / `ItemBuffer`、
  优雅退出、`AirSpider`
- **网络层** —— `Request` / `Response`（w3lib 编码检测 + parsel 选择器）、`Downloader` ABC +
  `HttpxDownloader`、重试与超时
- **数据与去重** —— `Item` / `UpdateItem`、内存布隆过滤器、
  `Console` / `CSV` / `Mongo` 管道
- **浏览器渲染** —— `PlaywrightDownloader` + 渲染池，`Request(render=True)`
- **中间件与代理** —— `DownloaderMiddleware` 链、`ProxyPool` ABC + `ApiProxyPool`
- **可观测性** —— `MetricsReporter` + Prometheus exporter、`AlertManager`（飞书 / 邮件 / 日志）
- **命令行** —— `mineworker create` 脚手架、`shell` 交互调试、`retry` 失败重放
- mkdocs-material 文档站

[Unreleased]: https://github.com/apersonw/mineworker/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/apersonw/mineworker/releases/tag/v0.5.0
[0.4.0]: https://github.com/apersonw/mineworker/releases/tag/v0.4.0
