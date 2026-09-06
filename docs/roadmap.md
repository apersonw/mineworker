# Roadmap

> 当前发行版本 **0.8.1** —— v1 + v2 的全部能力。变更明细见
> [CHANGELOG](https://github.com/apersonw/mineworker/blob/main/CHANGELOG.md)。

## 已完成

- **v1（0.3.0）** —— 轻量单机 AirSpider：运行时、网络、数据 / 去重、浏览器渲染、中间件 / 代理、指标 / 告警、CLI
- **v2.1** —— Redis 基础设施（连接 / 锁 / zset 队列 / 布隆 & 精确去重）
- **v2.2** —— [`Spider`（分布式）](distributed.md)：Redis 队列 + Redis 去重 + 断点续爬 + `start_requests` 一次性锁 + 多节点心跳结束检测 + 失败请求落 Redis
- **v2.3** —— [`TaskSpider`](distributed.md#taskspider)：从 Redis / DB 任务源持续拉任务，多节点分摊，`keep_alive` 常驻
- **v2.4** —— [账号 / Cookie 池](user-pool.md)：`LocalUserPool` / `GuestUserPool` / `RedisUserPool`；`user_pool()` + `check_login()` 钩子，掉登录自动换号重试
- **v2.5** —— [MySQL 管道](item-pipeline.md#mysql)：`MysqlPipeline`（`executemany` + `ON DUPLICATE KEY UPDATE` upsert）+ `mineworker create -i --table` 读 `SHOW FULL COLUMNS` 反射生成 Item
- **v2.6** —— [async 内核评估](async-kernel.md)：结论**不做全量 async 重写**（投入产出比不成立、破坏 feapder 心智兼容）；落地隔离的 `AsyncHttpxDownloader`（`DOWNLOADER_ASYNC=True`，事件循环线程 + 共享 `AsyncClient`，API 零改动）+ `HTTPX_HTTP2` 开关
- **v2.7** —— [`BatchSpider`](batch-spider.md)：MySQL 任务表状态机 + 批次记录表 + 周期批次调度 + 进度追踪 + 任务防丢；master（`start_monitor`）/ worker（`start`）分离，抽象 `BatchStore`（`MysqlBatchStore` / `MemoryBatchStore`）
- **管理平台** —— **MineWorkerHub**（独立仓库 / 独立服务，对标 feaplat）：
  项目托管（git / zip）· 任务调度（4 种）· Docker-per-task 实例 · SSE 实时日志 · 告警（飞书 / 邮件）·
  **懂 MineWorker 的监控看板**（直读爬虫 Redis 的队列深度 / 节点心跳 / 批次进度，抓 Prometheus 端点）
- **v2.8** —— 发布工程：首次发行到 PyPI（Trusted Publishing）、CHANGELOG、文档站自动部署
- **v2.9** —— [反爬对抗](anti-bot.md)：`CurlDownloader`（curl_cffi 伪装真实浏览器的
  TLS / HTTP2 指纹，`DOWNLOADER_IMPERSONATE`）+ 自动抑制矛盾的随机 UA +
  Cloudflare / Akamai 挑战页识别（`AntiBotError`，走既有重试与换代理）
- **v3.1** —— [吞吐画像与两处性能修复](async-kernel.md#实测2026-09)：建 `benchmarks/` 拿实测证据，
  **否决了 async 批量分发**（瓶颈不在线程模型）；缓存 SSL context（默认配置 **3.2× 吞吐**，
  语义零变化）、接通死配置 `USE_SESSION`
- **v3.0** —— [存储扩展](item-pipeline.md#postgresql)：抽出 `SqlPipeline` 基类，新增
  `PostgresPipeline`（`ON CONFLICT` 三种冲突模式）、`ElasticsearchPipeline`、
  `KafkaPipeline`；并补上**真实数据库集成测试**（Postgres 与 MySQL 都跑，CI 用
  service containers）—— 此前 `MysqlPipeline` 从没跑过真库
- **v4.2** —— [分布式真环境验证](distributed.md)：839 行分布式代码此前只用 fakeredis +
  单进程测过。补真 Redis + 真多进程集成测试（判据取自 HTTP 靶子而非框架自陈），
  **测出容器化部署下每次停节点都在丢任务**：框架只装了 `SIGINT` 处理器，
  而 `docker stop` 发的是 `SIGTERM` —— 实测 24 任务丢 20 个。已在 0.8.1 修复
- **v4.3** —— [跨节点全局限速](spider.md#跨节点全局限速)：`GLOBAL_THROTTLE` 把
  `DOWNLOAD_DELAY` 的记账搬到 Redis（Lua 原子取号，时钟取自服务端）。
  三节点实测：关闭时站点承受 10 请求/秒（配置值的 3 倍），打开后 4 请求/秒。
  补上了 v4.0 阶段 C 只能写进文档的那个限制

## 计划中

框架的能力面已经铺完（单机 → 分布式 → 批次 → 反爬 → 七种存储）。
下一程不加新玩法，补两块**生产环境才会疼的短板**。

### v4.0 —— 礼貌性与失败处理

> **动因**：0.6.0 把默认吞吐提高了约 3.2×，而框架**没有任何限速机制**——
> 唯一的节流是 `SPIDER_THREAD_COUNT` 默认 4。升级后用户会以 3 倍速度打目标站，
> 而且会把 429 错误页当成数据入库。这是上一次性能改动**加剧**了的责任缺口。

| 阶段 | 内容 | 关键设计张力 |
|---|---|---|
| ~~**A · 非 2xx 的正确处理**~~ ✅ | `validate()` 默认返回 `True`，于是 429 / 503 / 404 的响应体**直接进 `parse()` 被当成数据**。加状态码策略：`RETRY_STATUS_CODES`（默认 429/5xx→重试）、`ACCEPT_STATUS_CODES`（额外放行）、其余非 2xx 丢弃并计失败 | ⚠️ **这是破坏性变更**。现在依赖「错误页也进 parse」的爬虫会行为改变。需要决定：默认启用 + 大版本号，还是先 opt-in 一个版本再改默认 |
| ~~**B · 退避与 `Retry-After`**~~ ✅ | 429 / 503 重试时读 `Retry-After` 头（秒数或 HTTP 日期），按它等待而不是立刻重试；无该头时指数退避 + 抖动 | 与 `SPIDER_RETRY_INTERVAL`（现为 0.0）的关系要理顺，别出现两套退避 |
| ~~**C · per-domain 限速**~~ ✅ | `DOWNLOAD_DELAY`（同域两次请求最小间隔）+ `CONCURRENT_REQUESTS_PER_DOMAIN` + `RANDOMIZE_DOWNLOAD_DELAY`（避免整齐节奏本身成为特征） | 分布式模式下这是**每进程**限速，N 个节点就是 N 倍。全局限速要 Redis 令牌桶——先做进程内并**在文档里写清这个限制**，别让人误以为是全局的（间隔部分已由 v4.3 的 `GLOBAL_THROTTLE` 补上；并发上限仍是进程内的） |
| ~~**D · robots.txt**~~ ✅ | `urllib.robotparser` + 按域缓存，用框架自己的下载器抓取；同时读 `Crawl-delay` 喂给阶段 C | 默认值是个取舍：`True` 更礼貌但会让「抓自己站点」的用户困惑，`False` 则等于默认不合规。倾向默认 `True` + 一行显眼的关闭方法 |

### v4.1 —— 长跑生存

> **动因**：分布式爬虫常驻跑几天甚至几周，而这些机制一个都没有。

| 阶段 | 内容 | 备注 |
|---|---|---|
| ~~**E · 熔断**~~ ✅ | 同域连续失败 N 次 → 该域进冷却期，不再消耗重试配额去死磕一个挂掉的站点 | 与代理池配合：先换代理再熔断，避免把「代理坏了」误判成「站点挂了」 |
| ~~**F · 运行时长上限**~~ ✅ | `SPIDER_MAX_RUNTIME` / deadline，到点走既有的优雅退出（dump 未完成请求） | 复用 `DUMP_UNFINISHED_ON_EXIT` 既有路径 |
| ~~**G · 长跑内存画像**~~ ✅ | **实测无泄漏**：600s / 11 万请求后 RSS 在 ~127MB 收敛（最后 1/4 斜率 +0.22MB/分钟）；每线程边际成本仅 ~0.05MB。原先「65→144MB」是 `ru_maxrss` 峰值跨配置累积 + SSL context 未缓存两个假象叠加，后者已在 0.6.0 修掉 | 又一次「先测再改」避免了白干：本来要修的东西根本不存在 |

**先测后改**：v3.1 的教训是——文档里写着的推断可能是错的（「1 线程 1 在途」、
「线程数开到 100 无妨」都被实测推翻）。阶段 G 尤其要先拿数据。

## 设计约束

- 与 feapder **API 心智兼容**，不追求代码级兼容
- v1 不引入 Redis 运行时依赖（只保留接口）
- `Buffer` / `Collector` / `Dedup` 三个接口按「能整体换成 Redis 实现」设计
