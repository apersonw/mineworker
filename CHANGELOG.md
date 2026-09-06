# Changelog

本文件格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- **`GLOBAL_THROTTLE` —— 跨节点全局限速**。打开后 `DOWNLOAD_DELAY` 由 Redis 记账，
  所有节点共用一份「该域下次可请求的时刻」，N 个节点**合起来**才是配置的那个速率；
  429 的整域冷却同样全局生效，一个节点撞上限速所有节点一起避开。

    此前限速只在进程内生效，分布式起 N 个节点目标站就承受 N 倍 —— 这个限制一直
    只能写在文档里。三节点实测（`DOWNLOAD_DELAY=0.3`，判据取自 HTTP 靶子记录的
    到达时刻）：关闭时峰值 **10 请求/秒**（正好是配置值的 3 倍），打开后 **4 请求/秒**。

    取号用 Lua 保证原子，一次往返就算出准确的等待时长，不做「没令牌就重试」的轮询；
    时钟取自 Redis 服务端，避免节点间时钟偏移变成限速误差。
    Redis 不可用时**退回进程内限速**（不是退回不限速）并告警一次。

    默认关闭；并发上限 `CONCURRENT_REQUESTS_PER_DOMAIN` 仍是进程内的。

## [0.8.1] - 2026-09-07

分布式节点的数据丢失修复。**容器化部署的用户建议尽快升级。**

### 修复

- **节点被 `SIGTERM` 停止时会永久丢失已领取的任务** —— 框架只安装了 `SIGINT`
  处理器。`Collector` 一次从 Redis 领走最多 `COLLECTOR_TASK_COUNT`（默认 100）个任务，
  而队列用 `zpopmin`（取走即删）；`SIGTERM` 没有处理器 → 进程直接终止 →
  `_on_shutdown` 里「把本地缓冲推回 Redis」的逻辑没机会执行。

    实测 24 个任务的场景：`SIGTERM` 丢 20 个，`SIGINT` 全数恢复。而 `SIGTERM` 正是
    `docker stop` / Kubernetes 驱逐 / `systemctl stop` 所发的信号 —— 容器化部署下
    每次停节点都在丢任务。现在两个信号都走优雅停止。

- `robots.txt` 规则判定异常时不再完全静默 —— 此前 `can_fetch` 抛异常会直接放行
  且无任何日志，若对每个 URL 都抛异常会「全部放行」而无人察觉。现在告警一次。

### 说明

新增真 Redis + **真多进程**的集成测试。此前 839 行分布式代码全部只用 `fakeredis` +
单进程测过 —— 既没有真正的并发竞争，也不跨进程。上面那个 `SIGTERM` 缺陷就是它抓出来的。

### 新增

- **`examples/`** —— 可以直接跑的完整示例。首个是
  [`books_toscrape.py`](https://github.com/apersonw/mineworker/tree/main/examples)：
  两级抓取（列表页翻页 → 详情页）、`cb_kwargs` 传状态、`Item` + `__unique_key__` 去重，
  以及真实站点上该配的礼貌性设置。抓的是 Zyte 专为爬虫练习搭建的站点，可以放心跑。

    配套两层防腐测试：结构检查（不联网，进 CI）+ 真跑一遍（`network` 标记，
    不进 CI）—— 站点改版让选择器失效时，只有后者能发现。

## [0.8.0] - 2026-09-06

长跑生存：目标站挂了别死磕，定时任务跑够能停。

### 新增

- **按域熔断** —— 同域**连续**失败到 `CIRCUIT_FAILURE_THRESHOLD`（默认 10）次时，
  该域进入 `CIRCUIT_COOLDOWN`（默认 60s）冷却，所有工作线程一起避让
  （复用 per-domain 限速的整域降速机制）。

    **只数「站点不健康」的信号**：网络错误、429、5xx。**404 等 4xx 不计** ——
    按 ID 顺序探测时连续几十个 404 很正常，拿它跳闸会把正常爬取搞瘫；
    解析异常 / 校验失败同样不计，那是爬虫自己的问题。

    计数发生在**重试耗尽之后**，这样代理池有机会先轮换出口 ——
    「代理坏了」会被重试吸收，只有站点真的挂了才会连续走到这一步。

- **运行时长上限** —— `SPIDER_MAX_RUNTIME`（默认 `0` = 不限）。到点走优雅停止：
  flush 缓冲区、dump 未完成请求，然后**正常返回不抛异常** ——
  定时任务「跑够就停」不该被当成错误。

### 说明

顺带做了一次长跑内存画像，结论是**没有泄漏**：16 线程跑 600 秒、约 11 万请求后
RSS 在 ~127MB 收敛（最后 1/4 斜率 +0.22 MB/分钟），每线程边际成本约 0.05MB。
工具在 [`benchmarks/soak.py`](https://github.com/apersonw/mineworker/blob/main/benchmarks/soak.py)。

## [0.7.0] - 2026-09-05

礼貌性与失败处理。**含一处破坏性变更**，升级前请读下面第一节。

### ⚠️ 破坏性变更：非 2xx 响应不再进 `parse()`

0.6.0 及以前框架**完全不检查状态码**：`validate()` 默认返回 `True`，于是
429 / 503 / 404 的响应体直接进 `parse()` 被当成数据 —— 被限速时不但不退避，
还会把限速提示页入库，然后继续重试。

0.7.0 起：

| 状态码 | 处理 |
|---|---|
| 2xx / **3xx** | 正常进 `parse()`（3xx 能到回调说明你显式关了 `allow_redirects`） |
| 429 / 500 / 502 / 503 / 504 | 重试（可配 `RETRY_STATUS_CODES`） |
| 其余非 2xx | 判失败，走 `failed_request()` 钩子 |

**迁移**：
- 想让 `parse()` 继续收到 404 之类 → `ACCEPT_STATUS_CODES = [404]`
- 想完全回到旧行为 → `CHECK_STATUS_CODE = False`

### 新增

- **robots.txt** —— `ROBOTS_OBEY`（库默认 `False`，但 `mineworker create -p` 生成的
  项目配置里写 `True`，新项目开箱合规）、`ROBOTS_USER_AGENT`、`ROBOTS_CACHE_TTL`。

    按域缓存，多线程首访也只抓一次。被禁止的 URL 不产生请求、计入结束行的
    「robots 拦截」、**不算失败**（有意跳过不该污染失败率）。
    robots.txt 的 `Crawl-delay` 会自动接管该域限速，取 `max(DOWNLOAD_DELAY, Crawl-delay)`。

    抓不到 robots.txt 时（404 / 5xx / 超时）**放行**并打 warning ——
    一次瞬时 500 不该让整个爬虫停摆。

    小数 `Crawl-delay` 自行解析：标准库 `RobotFileParser` 只接受整数
    （用 `isdigit()` 判断），会静默丢弃 `Crawl-delay: 0.5` —— 那等于爬得比站点
    要求的还快。

- **per-domain 限速** —— 按域名分账的并发上限与请求间隔：
  `CONCURRENT_REQUESTS_PER_DOMAIN`（默认 `8`）、`DOWNLOAD_DELAY`（默认 `0`，不限）、
  `RANDOMIZE_DOWNLOAD_DELAY`（默认 `True`）。

    默认上限 8 大于默认线程数 4，所以**对默认配置无感** —— 它是调大
    `SPIDER_THREAD_COUNT` 时的安全网，而不是给所有人降速。

    收到 429 / 503 的 `Retry-After` 时，冷却作用在**整个域名**上，所有工作线程一起
    避开（只让撞上的那个线程等，其余线程会继续满速打同一个域，退避形同虚设）。

    ⚠️ **这是进程内限速**：分布式起 N 个节点，目标站承受 N 倍。全局限速需要 Redis
    令牌桶，尚未实现 —— 多节点部署请自行按节点数折算。

- **`Retry-After` 退避** —— 429 / 503 重试时读该头（秒数与 HTTP-date 都支持），
  按服务端要求等待。超过 `RETRY_AFTER_MAX`（默认 60s）则不再等待、直接判失败 ——
  等十分钟不值得占着一个工作线程
- **指数退避** —— `RETRY_BACKOFF > 0` 时按 `base × 2^(重试次数-1)` 等待并加抖动
  （抖动避免多个 worker 同步重试），封顶 `RETRY_AFTER_MAX`
- 新异常 `HttpStatusError(RequestError)`，带 `.status_code`

等待时长**只有一个计算入口**，优先级固定：`Retry-After` > 指数退避 > `SPIDER_RETRY_INTERVAL`。

### 说明

这一版的动因是 0.6.0：它把默认吞吐提高了约 3.2×，而当时框架**没有任何限速与失败处理**。
更快地打目标站、同时把错误页当数据入库，是需要马上补上的责任缺口。
per-domain 限速与 robots.txt 在后续版本。

## [0.6.0] - 2026-09-05

存储扩展 + 一次由实测驱动的性能修复。**升级即得约 3.2× 吞吐，无需改任何配置。**

### 性能

- **默认配置下吞吐提升约 3.2×**（50ms 延迟、32 线程：97 → 308 QPS）——
  缓存 `SSLContext`。`httpx.Client()` 每次构造都会新建 SSL context（加载 CA 包，
  实测 **32.9ms/个**），而下载器默认每个请求新建一个 Client，于是这 33ms 成了
  每请求的固定开销 —— 框架最大的单项成本。缓存后降到 **0.4ms**。

    **抓取语义零变化**：`SSLContext` 是无状态配置对象，cookie 仍然每请求隔离。
    这与「共享 `Client`」不同 —— 后者会连 cookie jar 一起共享。

- 顺带解决了「线程越多越慢」：那 33ms 的 CA 解析占着 GIL，本身就是争用源。
  修复前吞吐在 ~100 QPS 封顶，现在随线程数单调增长（4→128 线程：67 → 555 QPS）

### 新增

- **PostgreSQL 管道** —— `PostgresPipeline`（psycopg 3）。`ON CONFLICT` 三种模式：
  `nothing`（默认，冲突跳过）/ `update`（upsert，需 `POSTGRES_CONFLICT_TARGET`）/
  `error`。需 `pip install "mineworker[postgres]"`
- **Elasticsearch 管道** —— `helpers.bulk` 批量写，`__update_key__` 拼 `_id` 做 upsert
- **Kafka 管道** —— `table_name` 当 topic。它是投递而非存储，不支持 `UpdateItem`
- 抽出 `SqlPipeline` 基类，MySQL / PostgreSQL 共用同一套写入骨架
- **真实数据库集成测试** —— Postgres 与 MySQL 跑同一组用例，CI 用 service containers
  （`MysqlPipeline` 此前从没跑过真库）
- [`benchmarks/`](https://github.com/apersonw/mineworker/tree/main/benchmarks) 吞吐画像套件

### 修复

- **`setting.USE_SESSION` 是死配置** —— 它在 `setting.py` 有定义、文档里也写着
  「复用 httpx 连接」，但框架代码**从没读过它**，只有 `Request(use_session=)` 生效。
  现在两者都生效（请求级优先）。默认仍为 `False`：开启会让 cookie 跨请求共享

### 说明

0.4.0 曾记载「工作线程是 1 线程 1 在途」并建议「调大 `SPIDER_THREAD_COUNT` 到 ~100」。
实测表明**两者都不准确**：时间加权平均在途只有线程数的 15%–58%，而线程数超过某点后
效率显著下降。同时 **async 批量分发已被实测否决**（瓶颈不在线程模型）。
完整数据见 [async 内核评估](https://apersonw.github.io/mineworker/async-kernel/#实测2026-09)。

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

[Unreleased]: https://github.com/apersonw/mineworker/compare/v0.8.1...HEAD
[0.8.1]: https://github.com/apersonw/mineworker/releases/tag/v0.8.1
[0.8.0]: https://github.com/apersonw/mineworker/releases/tag/v0.8.0
[0.7.0]: https://github.com/apersonw/mineworker/releases/tag/v0.7.0
[0.6.0]: https://github.com/apersonw/mineworker/releases/tag/v0.6.0
[0.5.0]: https://github.com/apersonw/mineworker/releases/tag/v0.5.0
[0.4.0]: https://github.com/apersonw/mineworker/releases/tag/v0.4.0
