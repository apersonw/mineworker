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
DUMP_UNFINISHED_ON_EXIT: bool = True  # 中断退出时把未完成请求 dump 到 FAILED_REQUEST_PATH

# ---- 请求 ----
REQUEST_TIMEOUT: float = 22.0
RANDOM_USER_AGENT: bool = True
USE_SESSION: bool = False

# ---- per-domain 限速（按域名分账；⚠️ 进程内生效，分布式 N 个节点就是 N 倍）----
CONCURRENT_REQUESTS_PER_DOMAIN: int = 8  # 单域最大在途请求数；0 = 不限
# 默认 8 > 默认线程数 4，所以对默认配置无感 —— 它是调大线程数时的安全网
DOWNLOAD_DELAY: float = 0.0  # 同域两次请求的最小间隔（秒）；0 = 不限
RANDOMIZE_DOWNLOAD_DELAY: bool = True  # 给上面的间隔加 ±50% 抖动（整齐节奏本身是机器人特征）

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
DOWNLOADER_ASYNC_CONCURRENCY: int = 200  # async 下载器的最大在途请求数（信号量 + 连接池上限）
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

# ---- Item / 管道 ----
ITEM_MAX_CACHED_COUNT: int = 5000  # ItemBuffer 达到此量立即 flush
ITEM_PIPELINES: list[str] = ["mineworker.pipelines.console.ConsolePipeline"]
ITEM_DEFAULT_TABLE: str = "items"  # 直接 yield dict（非 Item）时落库的表名
ITEM_FILTER_ENABLE: bool = True  # 是否对 Item 做去重（按 fingerprint）
CSV_OUTPUT_DIR: str = "."  # CsvPipeline 输出目录
FAILED_ITEM_PATH: str = "failed_items.jsonl"
FAILED_REQUEST_PATH: str = "failed_requests.jsonl"

# ---- 去重 ----
# memory（进程内布隆）| lite（进程内精确 set）| redis（Redis 布隆）| redis-set（Redis 精确）
DEDUP_FILTER: str = "memory"
DEDUP_TO_MD5: bool = True  # Dedup 直接传入原始值时是否先 md5
DEDUP_ERROR_RATE: float = 1e-6
DEDUP_INITIAL_CAPACITY: int = 1_000_000

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
