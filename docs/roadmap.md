# Roadmap

当前（0.3.0）是轻量单机版（AirSpider）。架构上已为下面这些预留接口。

## v2

| 能力 | 说明 |
|---|---|
| **`Spider`（分布式）** | 内存队列 → Redis zset；内存去重 → Redis 可扩展布隆；断点续爬；多节点；`start_requests` 一次性锁 |
| **`TaskSpider`** | 周期性从 Redis / Mongo / MySQL 拉种子任务 |
| **账号 / Cookie 池** | guest / normal / gold 三档，登录态管理 |
| **MySQL 管道** | + `mineworker create -i` 读库表反射生成 Item |
| **async 内核** | 评估用 async httpx 替换线程模型 |

## v3

| 能力 | 说明 |
|---|---|
| **`BatchSpider`** | 批次记录表、进度追踪、定时批次、任务防丢 |
| **管理平台** | 部署 / 调度 / 日志 / 报警 Web UI，独立服务 |

## 设计约束

- 与 feapder **API 心智兼容**，不追求代码级兼容
- v1 不引入 Redis 运行时依赖（只保留接口）
- `Buffer` / `Collector` / `Dedup` 三个接口按「能整体换成 Redis 实现」设计
