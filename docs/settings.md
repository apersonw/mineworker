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
| `USE_SESSION` | `False` | 复用 httpx 连接 |
| `DOWNLOADER_MIDDLEWARES` | `[]` | 下载中间件点号路径 |
| `DOWNLOADER_ASYNC` | `False` | 普通请求走 [`AsyncHttpxDownloader`](async-kernel.md)（共享连接池 / HTTP/2） |
| `DOWNLOADER_ASYNC_CONCURRENCY` | `200` | async 下载器最大在途请求数 |
| `HTTPX_HTTP2` | `False` | httpx 开 HTTP/2（需 `pip install "httpx[http2]"`） |

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

## 代理 / 渲染 / 指标 / 告警

见 [中间件与代理](middleware-proxy.md)、[浏览器渲染](render.md)、[监控与调试](observability.md)。

## 日志

| 配置 | 默认 |
|---|---|
| `LOG_LEVEL` | `"INFO"` |
| `LOG_FILE` | `None`（只输出到 stderr） |
| `LOG_ROTATION` / `LOG_RETENTION` | `"50 MB"` / `"10 days"` |
