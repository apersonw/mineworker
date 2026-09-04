# Roadmap

## 已完成

- **v1（0.3.0）** —— 轻量单机 AirSpider：运行时、网络、数据 / 去重、浏览器渲染、中间件 / 代理、指标 / 告警、CLI
- **v2.1** —— Redis 基础设施（连接 / 锁 / zset 队列 / 布隆 & 精确去重）
- **v2.2** —— [`Spider`（分布式）](distributed.md)：Redis 队列 + Redis 去重 + 断点续爬 + `start_requests` 一次性锁 + 多节点心跳结束检测 + 失败请求落 Redis
- **v2.3** —— [`TaskSpider`](distributed.md#taskspider)：从 Redis / DB 任务源持续拉任务，多节点分摊，`keep_alive` 常驻

## 计划中

| 能力 | 说明 |
|---|---|
| **账号 / Cookie 池** | guest / normal / gold 三档，登录态管理 |
| **MySQL 管道** | + `mineworker create -i` 读库表反射生成 Item |
| **async 内核** | 评估用 async httpx 替换线程模型 |
| **`BatchSpider`** | 批次记录表、进度追踪、定时批次、任务防丢 |
| **管理平台** | 部署 / 调度 / 日志 / 报警 Web UI |

## 设计约束

- 与 feapder **API 心智兼容**，不追求代码级兼容
- v1 不引入 Redis 运行时依赖（只保留接口）
- `Buffer` / `Collector` / `Dedup` 三个接口按「能整体换成 Redis 实现」设计
