# Roadmap

> 当前发行版本 **0.5.0** —— v1 + v2 的全部能力。变更明细见
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

## 计划中

| 能力 | 说明 |
|---|---|
| **存储扩展** | PostgreSQL / Elasticsearch / Kafka 管道，抽一层批量写基类复用 `MysqlPipeline` 的套路 |
| **async 批量分发** | 突破 worker「1 线程 1 在途」天花板。**前置条件**：先有 benchmark 拿到实测证据，见 [async 内核评估](async-kernel.md#何时重新评估) |

## 设计约束

- 与 feapder **API 心智兼容**，不追求代码级兼容
- v1 不引入 Redis 运行时依赖（只保留接口）
- `Buffer` / `Collector` / `Dedup` 三个接口按「能整体换成 Redis 实现」设计
