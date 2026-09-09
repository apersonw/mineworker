"""框架默认配置 + 分层加载。

优先级（后者覆盖前者）：

    1. 本模块定义的框架默认值
    2. 项目配置文件：环境变量 ``MINEWORKER_SETTING`` 指定的 .py 文件，
       否则当前工作目录下的 ``setting.py`` / ``settings.py``
    3. 环境变量 ``MINEWORKER_<KEY>``（按默认值类型自动转换）

``import mineworker`` 时会自动执行一次 :func:`reload`。测试或运行期改动了
环境 / 配置文件后，再次调用 :func:`reload` 即可重新应用；:func:`apply` 用于
合并爬虫的 ``__custom_setting__``。
"""

from __future__ import annotations

import copy
import json
import os
import runpy
import warnings
from pathlib import Path
from typing import Any

# ======================================================================
# 框架默认值
# ======================================================================
PROJECT_NAME: str = "mineworker"

# ---- 日志 ----
LOG_LEVEL: str = "INFO"
LOG_FILE: str | None = None
LOG_COLOR: bool = True
LOG_ROTATION: str = "50 MB"
LOG_RETENTION: str = "10 days"

# ---- 调度 / 运行时 ----
SPIDER_THREAD_COUNT: int = 4
SPIDER_MAX_RETRY_TIMES: int = 3
SPIDER_RETRY_INTERVAL: float = 0.0  # 重试前等待（秒）

# ---- 状态码策略（0.7.0 起默认开启，行为与 0.6.0 不同）----
# 此前不检查状态码：429 / 503 / 404 的响应体会直接进 parse() 被当成数据。
# 现在 2xx/3xx 放行、429 与 5xx 重试、其余判失败。设 False 回到旧行为。
CHECK_STATUS_CODE: bool = True
RETRY_STATUS_CODES: list[int] = [429, 500, 502, 503, 504]
ACCEPT_STATUS_CODES: list[int] = []  # 除 2xx/3xx 外还当成功的码，如 [404] 让 parse 自己处理
# 429 / 503 的 Retry-After 最多认多久（秒）；超过则不再等待、直接判失败。0 = 不读该头
RETRY_AFTER_MAX: float = 60.0
# 指数退避基数（秒）：>0 时按 base * 2**(retry-1) 等待并加抖动，封顶 RETRY_AFTER_MAX。0 = 关
RETRY_BACKOFF: float = 0.0

# ---- 分布式 Spider ----
SPIDER_KEEP_ALIVE: bool = False  # True = 爬完不退出，继续轮询队列（配合 TaskSpider / 常驻 worker）
SPIDER_SEED_LOCK_TTL: int = 86400  # start_requests 一次性锁的 TTL（秒）
HEARTBEAT_INTERVAL: float = 3.0  # 节点心跳写入间隔（秒）
HEARTBEAT_STALE: float = 15.0  # 超过此秒数没心跳的节点视为已死

# ---- TaskSpider ----
TASK_POLL_INTERVAL: float = 2.0  # 轮询任务源的间隔（秒）
TASK_BATCH_SIZE: int = 100  # 单次拉取多少个任务
TASK_EXHAUST_POLLS: int = 3  # 连续这么多次拉不到任务，视为任务耗尽（keep_alive=False 时据此退出）

# ---- BatchSpider（批次采集，需 pip install "mineworker[redis,mysql]"）----
BATCH_INTERVAL: float = 7.0  # 批次间隔
BATCH_INTERVAL_UNIT: str = "day"  # day | hour
BATCH_MONITOR_INTERVAL: float = 10.0  # master 巡检间隔（秒）
BATCH_LOST_TASK_STALE: float = 600.0  # 任务卡在「处理中」超过这么久 → 重置回「待处理」
BATCH_PUSH_LIMIT: int = 5000  # master 单次最多认领 / 推送多少任务
BATCH_TASK_ID_FIELD: str = "id"  # 任务表主键列名
BATCH_TASK_STATE_FIELD: str = "batch_status"  # 状态列（0 待处理 / 1 完成 / 2 处理中 / -1 失败）
BATCH_TASK_TIME_FIELD: str = "update_time"  # 任务表更新时间列（防丢检测用）
COLLECTOR_TASK_COUNT: int = 100  # collector 单次从队列取多少任务
REQUEST_BUFFER_MAX_CACHED: int = 1000  # RequestBuffer 达到此量立即 flush
BUFFER_FLUSH_INTERVAL: float = 0.1  # RequestBuffer / ItemBuffer flush 轮询间隔
DONE_CHECK_TIMES: int = 3  # 结束检测连续复查次数
DONE_CHECK_INTERVAL: float = 0.5  # 每次复查间隔（秒）
# 启动宽限：节点在**从没拿到过任何任务**之前，至少要等这么久才允许判定「抓完了」。
#
# 多节点同时启动时只有一个能拿到种子锁，其余节点看到的是空队列 —— 而默认
# DONE_CHECK_TIMES × DONE_CHECK_INTERVAL 只有 1.5 秒，播种节点那时往往还没把种子
# 推进队列。心跳也挡不住：播种节点在那一刻的 pending 同样是 0，它自己还没开始拉活。
# 实测两个容器同秒启动，第二个节点 1 秒后就退出、0 个请求 —— 配 N 个节点
# 实际只有 1 个在干活，而且没有任何报错。
#
# 只有「一个任务都没见过」的节点付这个等待成本；拿到过活之后就按原规则判定。
SPIDER_STARTUP_GRACE: float = 10.0

# 任务租约：节点从 Redis 领走任务后，最多允许它「在途」这么久。
#
# 队列用 zpopmin —— 取走即删，任务进了某个节点的内存后 Redis 里就不存在了。
# 进程被 SIGKILL（OOM Killer / 断电 / docker kill）硬杀时，优雅停止与退出落盘
# 都轮不到执行，这些任务就随进程消失。实测 24 个任务被硬杀后只剩 5 个。
#
# 租约到期后任意节点都可以把它放回队列。**代价是「至少一次」语义**：节点只是卡住
# （长 GC、慢下载）而非死了的话，同一个任务会被处理两遍 —— 靠请求去重挡重复。
# 设 0 关闭租约（回到取走即删的老行为）。
SPIDER_TASK_LEASE: float = 600.0
DUMP_UNFINISHED_ON_EXIT: bool = True  # 中断退出时把未完成请求 dump 到 FAILED_REQUEST_PATH

# ---- 请求 ----
REQUEST_TIMEOUT: float = 22.0
RANDOM_USER_AGENT: bool = True
USE_SESSION: bool = False
# 开 USE_SESSION 且用代理池时，最多同时缓存多少个「每代理一个」的连接池。
# 代理池可能有上千个代理，无上限缓存会把连接和文件描述符耗光
SESSION_CACHE_SIZE: int = 16
# 每个「代理连接池」最多服务多少个线程，超出就再开一片。
#
# 一个 httpx.Client 被太多线程共用时，连接池本身成为争用点：实测（https、单代理、
# 每格多轮，靶子天花板已用裸 asyncio 确认远高于被测）共用一个 client 的吞吐
# 到 ~32 线程见顶后**掉头向下** —— 48 线程 248 QPS、64 线程 172、96 线程 105，
# 比「每请求新建一个 client」还慢。分片后：48 线程 563、64 线程 492、96 线程 434。
#
# 32 是实测的拐点。线程数不超过它时 K=1，与不分片完全一致。
# 设成 0 关闭分片（回到「每代理一个 client」）。
#
# **分片不改变 cookie 语义**：同一个代理的所有分片共用一个 CookieJar
# （httpx 收到裸 CookieJar 时按引用使用，且 CookieJar 自带锁）。
SESSION_SHARD_THREADS: int = 32

# ---- per-domain 限速（按域名分账）----
CONCURRENT_REQUESTS_PER_DOMAIN: int = 8  # 单域最大在途请求数；0 = 不限
# 默认 8 > 默认线程数 4，所以对默认配置无感 —— 它是调大线程数时的安全网
DOWNLOAD_DELAY: float = 0.0  # 同域两次请求的最小间隔（秒）；0 = 不限
RANDOMIZE_DOWNLOAD_DELAY: bool = True  # 给上面的间隔加 ±50% 抖动（整齐节奏本身是机器人特征）
# 上面两项默认只在**进程内**生效，分布式 N 个节点就是 N 倍。打开下面这项后，
# DOWNLOAD_DELAY 改由 Redis 全局记账，N 个节点合起来才是配置的那个速率。
# 需要 REDIS_URL；Redis 不可用时自动退回进程内限速（不会变成不限速）。
# 注意并发上限（CONCURRENT_REQUESTS_PER_DOMAIN）仍是进程内的。
GLOBAL_THROTTLE: bool = False

# ---- 资源边界（响应体大小 / 类型）----
# 框架此前会把**任何**响应整个读进内存，不看大小也不看类型。实测 200MB 的响应
# 让进程 RSS 涨 618MB（bytes 一份、.text 解码又一份），4 个线程同时撞上 ~2.5GB ——
# 容器里就是 OOM，而 OOM Killer 发 SIGKILL，会绕过优雅停止把已领取的任务打丢。
MAX_RESPONSE_SIZE: int = 32 * 1024 * 1024  # 响应体上限（字节）；0 = 不限
# 按**解压后**字节数计：Content-Length 报的是压缩后大小，只看它会被 gzip 炸弹绕过
ALLOWED_CONTENT_TYPES: list[str] = []  # Content-Type 白名单前缀；空 = 不过滤
# 例：["text/", "application/json", "application/xml"]。命中不了的响应**不读 body
# 直接断开**，省的是带宽。默认空是因为有人就是故意抓 PDF / 图片的

# ---- robots.txt ----
# 库默认 False（把 MineWorker 当库嵌入、抓自己站点/内网时不该被意外拦）；
# `mineworker create -p` 生成的项目配置里默认写 True，新项目开箱合规。
ROBOTS_OBEY: bool = False
# 按哪个 User-Agent 匹配规则。默认 "*"（通配组）：框架默认随机 UA，
# 每个请求的 UA 都不同，按具体 UA 匹配没有意义。
ROBOTS_USER_AGENT: str = "*"
ROBOTS_CACHE_TTL: float = 3600.0  # robots.txt 缓存时长（秒），0 = 永不过期

# ---- 熔断（目标站挂了就别再死磕）----
# 同域连续失败到阈值 → 该域冷却一段时间，所有工作线程一起避让。
# 只数「站点不健康」的信号（网络错误 / 5xx / 429）；404 等 4xx 不计 ——
# 按 ID 顺序探测时连续几十个 404 很正常，拿它跳闸会把正常爬取搞瘫。
CIRCUIT_FAILURE_THRESHOLD: int = 10  # 0 = 关闭熔断
CIRCUIT_COOLDOWN: float = 60.0  # 跳闸后该域冷却多久（秒）

# ---- 运行时长上限 ----
# 到点走优雅停止（flush 缓冲区、dump 未完成请求）并正常返回，不抛异常 ——
# 定时任务「跑够一小时就停」不该被当成错误。0 = 不限。
SPIDER_MAX_RUNTIME: float = 0.0

# ---- 下载器 ----
# True = 普通请求走 AsyncHttpxDownloader：一个事件循环线程 + 共享 AsyncClient 承载所有在途连接
# （连接池 / keep-alive / HTTP/2 被所有 worker 共享）。API 与线程模型不变。详见 docs/async-kernel.md
DOWNLOADER_ASYNC: bool = False
# async 下载器的信号量与连接池上限。
# **它不是「实际在途数」** —— 实际在途由 SPIDER_THREAD_COUNT 决定：
# 工作线程是同步阻塞地调 download() 的，一个线程同时只能有一个在途请求。
# 实测默认 200 时均在途只有 3~18，这个值从没成为过约束。
DOWNLOADER_ASYNC_CONCURRENCY: int = 200
# 每个事件循环最多服务多少工作线程，超出就再开一个循环。
#
# 「一个事件循环线程 + N 个线程阻塞提交」这个模式在 N 超过 ~24 时会**坍塌**：
# 实测 20 线程 342 QPS / 均在途 17.5，32 线程掉到 92 QPS / 均在途 4.7，
# 48 线程 60 QPS。而且**与本框架的逻辑无关** —— 把框架整个拿掉、只留
# 「一个 loop + N 线程 run_coroutine_threadsafe().result()」也一模一样地坍塌。
# 按 16 线程一个循环分片后：32 线程 506 QPS（5.7×）、48 线程 349（5.5×）、
# 64 线程 259（4.5×）。
#
# 设成 0 关闭分片（回到单个事件循环）。
ASYNC_THREADS_PER_LOOP: int = 16
HTTPX_HTTP2: bool = False  # httpx 开 HTTP/2（需 httpx[http2]），同步 / 异步下载器都生效

# ---- 反爬：TLS / HTTP2 指纹伪装（需 pip install "mineworker[curl]"）----
# 填浏览器名即启用，普通请求改走 CurlDownloader（libcurl-impersonate）。
# 例："chrome"（跟随 curl_cffi 的最新 Chrome）、"chrome131"、"safari17_0"、"firefox135"。
# 空串 = 关闭。可用 Request(impersonate=...) 按请求覆盖。详见 docs/anti-bot.md
DOWNLOADER_IMPERSONATE: str = ""

# 识别 Cloudflare / Akamai 挑战页等反爬拦截，命中则抛 AntiBotError（继承 RequestError，
# 走正常重试 + 换代理）。默认开：挑战页常返回 200 + 一段 JS，不识别就会被当成正常数据
# 静默入库。规则很保守（只认专有响应头 / 专有脚本标记），误伤了就设 False 关掉。
ANTIBOT_DETECT: bool = True

# ---- 下载中间件（点号路径，实现 process_request / process_response）----
DOWNLOADER_MIDDLEWARES: list[str] = []

# ---- 代理池 ----
PROXY_ENABLE: bool = False
PROXY_POOL: str = "mineworker.network.proxy_pool.api.ApiProxyPool"
PROXY_EXTRACT_API: str = ""  # 返回代理的 URL（每行一个，或 JSON 数组）
PROXY_MAX_USE_TIMES: int = 100  # 单个代理最多用多少次后轮换
PROXY_MIN_INTERVAL: float = 1.0  # 两次抓取代理列表的最小间隔（秒）
# 池空时最多等多久（秒）—— 供应商短暂断供期间不至于把任务耗完重试次数丢掉
PROXY_WAIT_TIMEOUT: float = 30.0
# 代理失败后的冷却时间（秒）—— **不是永久拉黑**。
# 原来一次 `report_bad` 就把代理永久加进黑名单，而拉列表时又拒绝放回黑名单里的，
# 于是单代理池被一次瞬时错误打空后**再也起不来**：配合 PROXY_ALLOW_DIRECT=False，
# 后面每个请求都先等满 PROXY_WAIT_TIMEOUT 再失败。不是崩溃，是静默降级。
# 而瞬时错误是常态 —— 实测健康的本地 tinyproxy 在 960 个请求里也重置了 3 条连接。
# 连续失败按 2 倍退避（60 → 120 → 240…），封顶 PROXY_BAN_MAX_SECONDS；
# 距上次失败超过封顶值即视为已恢复，退避重新从头算。
PROXY_BAN_SECONDS: float = 60.0
PROXY_BAN_MAX_SECONDS: float = 900.0  # 退避上限；PROXY_BAN_SECONDS=0 则回到「永久拉黑」
# 等不到代理时是否允许直连。默认 False：静默直连会把源 IP 暴露给目标站，
# 而那正是开代理池要避免的。设成 True 就是明确接受「有代理就用、没有就直连」
PROXY_ALLOW_DIRECT: bool = False

# ---- 账号 / Cookie 池 ----
# 池空时最多等多久（秒）—— 游客池能现登，拉黑也会到期
USER_POOL_WAIT_TIMEOUT: float = 10.0
# 拿不到账号时是否允许匿名发出。默认 False：需要登录的站会回一张登录墙，
# 那张页面会被当成数据存进库。设成 True 就是明确接受「有号更好、没号也能抓」
USER_POOL_ALLOW_ANONYMOUS: bool = False

# ---- Item / 管道 ----
ITEM_MAX_CACHED_COUNT: int = 5000  # ItemBuffer 达到此量立即 flush
ITEM_PIPELINES: list[str] = ["mineworker.pipelines.console.ConsolePipeline"]
ITEM_DEFAULT_TABLE: str = "items"  # 直接 yield dict（非 Item）时落库的表名
ITEM_FILTER_ENABLE: bool = True  # 是否对 Item 做去重（按 fingerprint）
CSV_OUTPUT_DIR: str = "."  # CsvPipeline 输出目录
FAILED_ITEM_PATH: str = "failed_items.jsonl"
FAILED_REQUEST_PATH: str = "failed_requests.jsonl"
# 启动时把 FAILED_REQUEST_PATH 里的请求重新灌回队列（走完整的下载 → 回调 → 落库）。
# `mineworker retry --requests` 只是探活，不会把数据抓回来 —— 这个才会
RETRY_FAILED_ON_START: bool = False

# ---- 响应缓存（开发调试用）----
# 写爬虫是来回试的过程：改一版选择器、重跑一遍，一天下来同一批页面可能被抓几十遍。
# 打开后第一遍照常抓，之后重跑直接读本地文件 —— 省时间，也是对目标站的礼貌。
#
# **默认关闭，且只该在开发期开** —— 跑生产时开着很容易出事：你以为在抓新数据，
# 实际读的是几天前的副本。只缓存 GET（POST 不幂等）和状态码正常的响应
# （把 429 存下来重放，等于每次重跑都在读那张限速页）。
RESPONSE_CACHE_ENABLE: bool = False
RESPONSE_CACHE_PATH: str = ".mineworker_cache"
RESPONSE_CACHE_EXPIRE: float = 3600.0  # 秒；0 = 不过期

# ---- 去重 ----
# memory（进程内布隆）| lite（进程内精确 set）| redis（Redis 布隆）| redis-set（Redis 精确）
DEDUP_FILTER: str = "memory"
DEDUP_TO_MD5: bool = True  # Dedup 直接传入原始值时是否先 md5
DEDUP_ERROR_RATE: float = 1e-6
DEDUP_INITIAL_CAPACITY: int = 1_000_000
# 布隆填充到这个比例就告警。默认 0.8 是为了**在误判率变糟之前**通知你 ——
# 实测容量 3 倍时就已经每 13 个新 URL 丢 1 个，等到 1.0 才报就太晚了
DEDUP_WARN_FILL_RATE: float = 0.8
# 布隆最多分几层。一层填满就加一层：容量 ×2、误判率 ×0.5，所以总误判率收敛到
# 目标值的 2 倍以内。代价是内存 —— 基础容量 100 万时：
#   1 层 = 100 万 / 3MB    4 层 = 1500 万 / 57MB
#   6 层 = 6300 万 / 260MB  8 层 = 2.55 亿 / 1.1GB
# 默认 4 层：容量 ×15 只花 57MB。**必须有顶** —— 无限加层就是把内存变成无界资源，
# 那正是 v4.4 刚从下载路径上清掉的东西。到顶后退化成单层并沿用超容告警。
DEDUP_MAX_LAYERS: int = 4

# ---- MongoDB ----
MONGO_URI: str = "mongodb://localhost:27017"
MONGO_DB: str = "mineworker"

# ---- MySQL（MysqlPipeline / create -i --table，需 pip install mineworker[mysql]）----
MYSQL_HOST: str = "localhost"
MYSQL_PORT: int = 3306
MYSQL_USER: str = "root"
MYSQL_PASSWORD: str = ""
MYSQL_DB: str = "mineworker"
MYSQL_POOL_SIZE: int = 5
MYSQL_UPDATE_ON_DUPLICATE: bool = True  # save_items 用 INSERT ... ON DUPLICATE KEY UPDATE

# ---- PostgreSQL（需 pip install "mineworker[postgres]"）----
POSTGRES_HOST: str = "localhost"
POSTGRES_PORT: int = 5432
POSTGRES_USER: str = "postgres"
POSTGRES_PASSWORD: str = ""
POSTGRES_DB: str = "mineworker"
POSTGRES_POOL_SIZE: int = 5
# 冲突处理：error 冲突即报错 / nothing 跳过（默认）/ update 更新（需 POSTGRES_CONFLICT_TARGET）
POSTGRES_ON_CONFLICT: str = "nothing"
POSTGRES_CONFLICT_TARGET: list[str] = []  # update 模式下的冲突列，通常是唯一索引的列

# ---- Elasticsearch（需 pip install "mineworker[elasticsearch]"）----
ELASTICSEARCH_HOSTS: list[str] = ["http://localhost:9200"]

# ---- Kafka（需 pip install "mineworker[kafka]"）----
KAFKA_BOOTSTRAP_SERVERS: list[str] = ["localhost:9092"]

# ---- Redis（分布式 Spider / 持久化去重）----
REDIS_URL: str = "redis://localhost:6379/0"
REDIS_KEY_PREFIX: str = "mineworker"  # 所有 Redis key 的命名空间前缀

# ---- 浏览器渲染（render=True，需 pip install mineworker[render]）----
WEBDRIVER: dict[str, Any] = {
    "pool_size": 1,  # 并发浏览器数（独立渲染线程，各自持有一个 chromium）
    "browser": "chromium",  # chromium | firefox | webkit
    "headless": True,
    "load_images": False,  # 拦截图片 / 字体 / 媒体，加速
    "timeout": 30,  # 秒，页面加载 / 等待选择器超时
    "render_time": 0,  # 加载后额外等待秒数
    "wait_until": "domcontentloaded",  # load | domcontentloaded | networkidle | commit
    "wait_for": None,  # 全局等待的 CSS 选择器（Request.wait_for 可覆盖）
    "user_agent": None,
    "proxy": None,  # http://user:pass@host:port
    "stealth": True,  # 注入基础反检测脚本
    "viewport": [1920, 1080],
}

# ---- 调试 ----
DEBUG: bool = False  # AirSpider(debug=True) 会置为 True：日志转 DEBUG、单线程

# ---- 指标 ----
METRICS_ENABLE: bool = False
METRICS_LOG_INTERVAL: float = 10.0  # 定时打印进度行的间隔（秒；0 = 关）
METRICS_PROMETHEUS_PORT: int = 0  # >0 且装了 prometheus-client 时起 exporter

# ---- 告警 ----
WARNING_ENABLE: bool = True  # 关掉则完全不告警
WARNING_FEISHU_WEBHOOK: str = ""
WARNING_DINGTALK_WEBHOOK: str = ""
WARNING_DINGTALK_SECRET: str = ""  # 钉钉「加签」模式的密钥；用「自定义关键词」则留空
WARNING_WECHAT_WEBHOOK: str = ""  # 企业微信群机器人
WARNING_EMAIL: dict[str, Any] = {}  # {host, port, user, password, to: [...], ssl: bool}
WARNING_INTERVAL: float = 300.0  # 同类告警的最小间隔（秒），防刷屏
WARNING_FAILED_RATE: float = 0.5  # 失败率阈值
WARNING_MIN_REQUESTS: int = 50  # 少于这么多请求不计算失败率
WARNING_FAILED_COUNT: int = 1000  # 失败请求数阈值
WARNING_STALL_SECONDS: float = 600.0  # 多久没有新的成功请求算卡死（0 = 关）


# ======================================================================
# 加载机制
# ======================================================================
_SETTING_KEYS: frozenset[str] = frozenset(
    name for name in tuple(globals()) if name.isupper() and not name.startswith("_")
)
_DEFAULTS: dict[str, Any] = {name: copy.deepcopy(globals()[name]) for name in _SETTING_KEYS}


def _coerce(current: Any, raw: str) -> Any:
    """把环境变量字符串按 `current` 的类型转换。"""
    if isinstance(current, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, (list, dict)):
        return json.loads(raw)
    return raw


def _apply(mapping: dict[str, Any]) -> None:
    g = globals()
    for key, value in mapping.items():
        if key.isupper() and not key.startswith("_"):
            g[key] = value


def _load_project_file() -> dict[str, Any]:
    override = os.environ.get("MINEWORKER_SETTING")
    if override:
        candidates = [Path(override)]
    else:
        cwd = Path.cwd()
        candidates = [cwd / "setting.py", cwd / "settings.py"]

    for path in candidates:
        if not path.is_file():
            continue
        try:
            namespace = runpy.run_path(str(path))
        except Exception as exc:  # 配置文件可能有任意错误
            warnings.warn(f"加载项目配置 {path} 失败：{exc!r}", stacklevel=3)
            return {}
        return {k: v for k, v in namespace.items() if k.isupper() and not k.startswith("_")}
    return {}


def _apply_env() -> None:
    g = globals()
    for key in _SETTING_KEYS:
        env_key = f"MINEWORKER_{key}"
        raw = os.environ.get(env_key)
        if raw is None:
            continue
        try:
            g[key] = _coerce(_DEFAULTS[key], raw)
        except ValueError as exc:  # int/float/json.loads 解析失败
            warnings.warn(f"环境变量 {env_key} 解析失败：{exc!r}", stacklevel=3)


def reload() -> None:
    """重置为默认值，再依次应用项目配置文件与环境变量。"""
    _apply({name: copy.deepcopy(value) for name, value in _DEFAULTS.items()})
    _apply(_load_project_file())
    _apply_env()


def apply(mapping: dict[str, Any]) -> None:
    """合并额外配置（供 Spider 的 ``__custom_setting__`` 使用）。"""
    _apply({k: v for k, v in mapping.items() if k.isupper() and not k.startswith("_")})


def as_dict() -> dict[str, Any]:
    """返回当前全部配置项的快照。"""
    g = globals()
    return {key: g[key] for key in sorted(_SETTING_KEYS)}
