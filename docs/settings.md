# 配置项

优先级（后者覆盖前者）：

1. 框架默认值（`mineworker/setting.py`）
2. 运行目录下的 `setting.py` / `settings.py`（或 `MINEWORKER_SETTING` 指定的文件）
3. 环境变量 `MINEWORKER_<KEY>`（按默认值类型自动转换，dict / list 传 JSON）

```bash
MINEWORKER_SPIDER_THREAD_COUNT=8 MINEWORKER_LOG_LEVEL=DEBUG python main.py
```

爬虫内 `__custom_setting__` 会在实例化时合并进全局配置。

## 调度 / 运行时

| 配置 | 默认 | 说明 |
|---|---|---|
| `SPIDER_THREAD_COUNT` | `4` | 工作线程数 |
| `SPIDER_MAX_RETRY_TIMES` | `3` | 单请求最大重试次数 |
| `SPIDER_RETRY_INTERVAL` | `0.0` | 重试前等待秒数 |
| `COLLECTOR_TASK_COUNT` | `100` | collector 单次从队列取多少任务 |
| `DONE_CHECK_TIMES` / `DONE_CHECK_INTERVAL` | `3` / `0.5` | 结束检测的复查次数与间隔 |
| `DUMP_UNFINISHED_ON_EXIT` | `True` | 中断时 dump 未完成请求 |

## 请求

| 配置 | 默认 | 说明 |
|---|---|---|
| `REQUEST_TIMEOUT` | `22.0` | 秒 |
| `RANDOM_USER_AGENT` | `True` | 自动注入随机 UA |
| `USE_SESSION` | `False` | 复用 httpx 连接（连同 cookie jar）。实测再增约 1.2× 吞吐；注意开启后 cookie 会跨请求共享 |
| `DOWNLOADER_MIDDLEWARES` | `[]` | 下载中间件点号路径 |
| `CONCURRENT_REQUESTS_PER_DOMAIN` | `8` | 单域最大在途；`0` = 不限。**进程内生效**，见[限速](spider.md#限速) |
| `DOWNLOAD_DELAY` | `0.0` | 同域两次请求最小间隔（秒）；`0` = 不限 |
| `RANDOMIZE_DOWNLOAD_DELAY` | `True` | 给上面的间隔加 ±50% 抖动 |
| `ROBOTS_OBEY` | `False` | 遵守 robots.txt。**脚手架生成的项目里默认 `True`**，见 [robots.txt](spider.md#robotstxt) |

> 结束行里的「robots 拦截」是「丢弃」的**子集**而非并列项 —— 被 robots 拦下的请求同时计入两者，求和会重复计。
| `ROBOTS_USER_AGENT` | `"*"` | 按哪个 UA 组匹配（随机 UA 下按具体 UA 匹配没有意义） |
| `ROBOTS_CACHE_TTL` | `3600.0` | robots.txt 缓存时长（秒），`0` = 永不过期 |
| `CIRCUIT_FAILURE_THRESHOLD` | `10` | 同域连续失败多少次跳闸；`0` = 关闭。**404 等 4xx 不计**，见[熔断](spider.md#熔断) |
| `CIRCUIT_COOLDOWN` | `60.0` | 跳闸后该域冷却多久（秒） |
| `SPIDER_MAX_RUNTIME` | `0.0` | 运行时长上限（秒），到点优雅停止；`0` = 不限 |
| `CHECK_STATUS_CODE` | `True` | **0.7.0 起默认开启**：非 2xx/3xx 不再进 `parse()`。设 `False` 回到旧行为 |
| `RETRY_STATUS_CODES` | `[429,500,502,503,504]` | 这些码触发重试 |
| `ACCEPT_STATUS_CODES` | `[]` | 除 2xx/3xx 外还当成功的码，如 `[404]` 让 `parse` 自己处理 |
| `RETRY_AFTER_MAX` | `60.0` | 429/503 的 `Retry-After` 最多认多久（秒）；超过判失败。`0` = 不读该头 |
| `RETRY_BACKOFF` | `0.0` | 指数退避基数（秒），`0` = 关，沿用 `SPIDER_RETRY_INTERVAL` |
| `DOWNLOADER_ASYNC` | `False` | 普通请求走 [`AsyncHttpxDownloader`](async-kernel.md)（共享连接池 / HTTP/2） |
| `DOWNLOADER_ASYNC_CONCURRENCY` | `200` | async 下载器最大在途请求数 |
| `HTTPX_HTTP2` | `False` | httpx 开 HTTP/2（需 `pip install "httpx[http2]"`） |
| `DOWNLOADER_IMPERSONATE` | `""` | 伪装浏览器 TLS / HTTP2 指纹，填 `"chrome"` 等即启用（需 `pip install "mineworker[curl]"`），见[反爬对抗](anti-bot.md) |
| `ANTIBOT_DETECT` | `True` | 识别 Cloudflare / Akamai 挑战页，命中抛 `AntiBotError`（走既有重试 + 换代理） |

## Item / 管道 / 去重

| 配置 | 默认 | 说明 |
|---|---|---|
| `ITEM_PIPELINES` | `["...ConsolePipeline"]` | 管道列表 |
| `ITEM_MAX_CACHED_COUNT` | `5000` | 达到即 flush |
| `ITEM_DEFAULT_TABLE` | `"items"` | 裸 dict 落库的表名 |
| `ITEM_FILTER_ENABLE` | `True` | Item 级去重开关 |
| `CSV_OUTPUT_DIR` | `"."` | CsvPipeline 输出目录 |
| `DEDUP_FILTER` | `"memory"` | `memory`（布隆）\| `lite`（精确）\| `redis` \| `redis-set` |
| `DEDUP_ERROR_RATE` | `1e-6` | 布隆误判率 |
| `MONGO_URI` / `MONGO_DB` | `localhost` / `mineworker` | |

## MySQL

`pip install "mineworker[mysql]"`。用于 `MysqlPipeline` 与 `create -i --table`。

| 配置 | 默认 | 说明 |
|---|---|---|
| `MYSQL_HOST` / `MYSQL_PORT` | `localhost` / `3306` | |
| `MYSQL_USER` / `MYSQL_PASSWORD` | `root` / `""` | |
| `MYSQL_DB` | `"mineworker"` | 库名 |
| `MYSQL_POOL_SIZE` | `5` | 连接池上限 |
| `MYSQL_UPDATE_ON_DUPLICATE` | `True` | `save_items` 用 `INSERT ... ON DUPLICATE KEY UPDATE` |

## PostgreSQL

`pip install "mineworker[postgres]"`。用于 `PostgresPipeline`。psycopg 是 LGPL-3.0，
详见[数据与去重](item-pipeline.md#postgresql)。

| 配置 | 默认 | 说明 |
|---|---|---|
| `POSTGRES_HOST` / `POSTGRES_PORT` | `localhost` / `5432` | |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | `postgres` / `""` | |
| `POSTGRES_DB` | `"mineworker"` | 库名 |
| `POSTGRES_POOL_SIZE` | `5` | 连接池上限 |
| `POSTGRES_ON_CONFLICT` | `"nothing"` | `error` 冲突报错 / `nothing` 跳过 / `update` upsert |
| `POSTGRES_CONFLICT_TARGET` | `[]` | `update` 模式下的冲突列，通常是唯一索引的列 |

## Elasticsearch / Kafka

| 配置 | 默认 | 说明 |
|---|---|---|
| `ELASTICSEARCH_HOSTS` | `["http://localhost:9200"]` | 需 `pip install "mineworker[elasticsearch]"` |
| `KAFKA_BOOTSTRAP_SERVERS` | `["localhost:9092"]` | 需 `pip install "mineworker[kafka]"` |

## 代理 / 渲染 / 指标 / 告警

见 [中间件与代理](middleware-proxy.md)、[浏览器渲染](render.md)、[监控与调试](observability.md)。

## 日志

| 配置 | 默认 |
|---|---|
| `LOG_LEVEL` | `"INFO"` |
| `LOG_FILE` | `None`（只输出到 stderr） |
| `LOG_ROTATION` / `LOG_RETENTION` | `"50 MB"` / `"10 days"` |
