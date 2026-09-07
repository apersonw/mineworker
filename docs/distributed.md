# 分布式（Spider）

`Spider` 把队列和去重都放到 Redis，多个进程 / 多台机器跑同一个爬虫、共享进度、断点续爬。

```bash
pip install "mineworker[redis]"
```

## 用法

和 `AirSpider` 一模一样，只是换个基类：

```python
import mineworker as mw


class NewsSpider(mw.Spider):
    __custom_setting__ = dict(REDIS_URL="redis://localhost:6379/0")

    def start_requests(self):
        for page in range(1, 100):
            yield mw.Request(f"https://example.com/news?p={page}", callback=self.parse)

    def parse(self, request, response):
        ...


if __name__ == "__main__":
    NewsSpider().start()
```

在多台机器 / 多个进程里跑同一份代码即可：

```bash
python main.py    # 机器 A
python main.py    # 机器 B —— 自动加入，一起消费队列
```

## 工作方式

| 机制 | 说明 |
|---|---|
| **队列** | Redis zset（`<prefix>:<redis_key>:z_requests`），score = priority |
| **去重** | Redis 布隆 / 精确 set（`DEDUP_FILTER=redis` / `redis-set`） |
| **种子一次性** | `start_requests` 靠 `SET NX EX` 锁，只有抢到锁的节点执行；其他节点直接消费队列 |
| **断点续爬** | 队列在 Redis，进程崩溃重启后队列还在，继续跑；崩溃节点采集器里没跑完的会在退出时推回队列 |
| **多节点结束检测** | 每个节点每 `HEARTBEAT_INTERVAL` 秒写一次心跳（含本节点在途数）。只有「队列空 + 所有活跃节点在途数都为 0」才判定结束 |
| **失败请求** | 重试耗尽的请求 RPUSH 到 `<prefix>:<redis_key>:failed_requests` |

`redis_key` 默认是爬虫类名，决定 Redis 命名空间。不同爬虫用不同 `redis_key`，
同一个爬虫的多个进程用同一个。

## 常驻模式

```python
NewsSpider(keep_alive=True).start()
```

爬完队列也不退出，继续轮询 —— 配合 `TaskSpider`（Roadmap）或一个长期运行的 worker 池。

## 配置

```python
REDIS_URL = "redis://:password@host:6379/0"
REDIS_KEY_PREFIX = "mineworker"      # 所有 key 的前缀
DEDUP_FILTER = "redis"               # redis（布隆）| redis-set（精确）
SPIDER_SEED_LOCK_TTL = 86400         # 种子锁 TTL（秒）；过期后下次启动会重新注入种子
HEARTBEAT_INTERVAL = 3.0
HEARTBEAT_STALE = 15.0               # 超过这个秒数没心跳的节点视为已死
SPIDER_KEEP_ALIVE = False
```

## 去重的容量

布隆过滤器是**固定容量**的，超容后误判率会急剧升高 —— 而在去重里，
「误判」意味着**一个从没抓过的 URL 被当成「已抓过」静默丢掉**。

实测（基础容量 10 万、目标误判率 `1e-6`，单层）：

| 已插入 | 倍数 | 误判率 | 实际后果 |
|---|---|---|---|
| 10 万 | 1× | 0% | 正常 |
| 30 万 | 3× | 7.2% | 每 13 个新 URL 丢 1 个 |
| 50 万 | 5× | **53.7%** | **一半的站点抓不到** |
| 80 万 | 8× | 92.8% | 基本不再发现新页面 |

所以布隆是**分层**的：一层填满就加一层，容量 ×2、误判率 ×0.5 ——
后者是关键，各层误判率成等比数列，**总误判率收敛到目标值的 2 倍以内**，
加多少层都不会失控。

```python
DEDUP_INITIAL_CAPACITY = 1_000_000   # 第一层的容量
DEDUP_MAX_LAYERS = 4                 # 最多几层
DEDUP_WARN_FILL_RATE = 0.8           # 填到八成就告警
```

代价是内存：

| 层数 | 累计容量 | 累计内存 | 总误判率上界 |
|---|---|---|---|
| 1 | 100 万 | 3MB | 1.0e-6 |
| **4（默认）** | **1500 万** | **57MB** | 1.88e-6 |
| 6 | 6300 万 | 260MB | 1.97e-6 |
| 8 | 2.55 亿 | 1.1GB | 1.99e-6 |

!!! warning "层数必须有顶"
    `DEDUP_MAX_LAYERS` 不是可选的保险 —— 让层数无限长下去就是把 Redis 内存
    变成一个**无界资源**，而那正是[资源边界](spider.md#资源边界)刚从下载路径上
    清掉的东西。到顶之后行为退化成单层布隆，并触发下面的告警。

    查询要遍历所有层，层数越多越慢，这是不宜开太大的另一个原因。

### 填满了会告警

填到 `DEDUP_WARN_FILL_RATE`（默认八成）就走[告警通道](observability.md)（飞书 / 邮件），
到顶后再写还会额外记一条日志。

这条告警是有来由的：在它之前**完全没有任何信号** —— 计数一直有记录但没人读，
统计里只显示「去重 N 条」，爬虫只是「提前结束了」或「少抓了很多」，查不出原因。

### 不想要误判就用精确去重

```python
DEDUP_FILTER = "redis-set"    # 一个 Redis SET，成员即指纹
```

不会误判，代价是内存随抓取量**线性**增长。这是取舍，不是「布隆坏了就换它」。

!!! note "升级不会让已有去重状态失效"
    第 0 层沿用原来的 key（`{name}:dedup:bloom`），所以从旧版本升上来的
    断点续爬不会从头再抓一遍。新增的层是 `:1`、`:2` ……

## 重新注入种子

想让爬虫重新从头跑（比如换了一批种子）：删掉种子锁和队列。

```bash
redis-cli DEL mineworker:NewsSpider:lock:seed mineworker:NewsSpider:z_requests
redis-cli DEL mineworker:NewsSpider:dedup:bloom \
  mineworker:NewsSpider:dedup:bloom:1 \
  mineworker:NewsSpider:dedup:bloom:2 \
  mineworker:NewsSpider:dedup:bloom:3 \
  mineworker:NewsSpider:dedup:bloom:count      # 顺便清去重（含各分层与计数）
```

---

# TaskSpider

有一堆待抓的 id / url，想用一个或多个常驻进程慢慢消费 —— 用 `TaskSpider`。
它不走 `start_requests`，而是一个后台线程每 `TASK_POLL_INTERVAL` 秒从任务源
（默认 Redis list）拉一批任务，对每个任务调 `task_requests(task)` 生成请求。

```python
import mineworker as mw


class ProductSpider(mw.TaskSpider):
    def task_requests(self, task):
        yield mw.Request(
            f"https://shop.com/p/{task['id']}",
            callback=self.parse,
            cb_kwargs={"task": task},
        )

    def parse(self, request, response, task):
        yield {"id": task["id"], "price": response.css(".price::text").get()}
```

生产任务（任意进程 / 脚本）：

```python
ProductSpider.push_tasks({"id": 1}, {"id": 2}, {"id": 3})
```

消费（一台或多台机器）：

```python
ProductSpider().start()               # 任务耗尽后退出
ProductSpider(keep_alive=True).start() # 常驻，队列空也不退出
```

多个节点 `lpop` 同一个 Redis list，任务天然分摊、不重复。运行中还能
`self.add_tasks(...)`（比如在 parse 里发现了新的待抓项）。

覆写 `fetch_tasks(self, limit)` 可换成从 MySQL / Mongo 查：

```python
class ProductSpider(mw.TaskSpider):
    def fetch_tasks(self, limit):
        rows = db.query("SELECT id FROM crawl_task WHERE status=0 LIMIT %s", limit)
        db.execute("UPDATE crawl_task SET status=1 WHERE id IN ...")   # 标记为处理中
        return [dict(r) for r in rows]
```

| 配置 | 默认 | 说明 |
|---|---|---|
| `TASK_POLL_INTERVAL` | `2.0` | 轮询任务源的间隔（秒） |
| `TASK_BATCH_SIZE` | `100` | 单次拉多少个 |
| `TASK_EXHAUST_POLLS` | `3` | 连续这么多次拉不到任务，视为耗尽（`keep_alive=False` 时据此退出） |

## 停止节点：用 SIGTERM 或 SIGINT，别用 SIGKILL

节点收到 `SIGTERM` / `SIGINT` 时会优雅停止：把 collector 与 buffer 里**已从 Redis
领走但还没跑完**的任务**推回队列**，交给其他节点或下次重启接管。

这一步不能省。`Collector` 一次会 `get_batch` 领走最多 `COLLECTOR_TASK_COUNT`（默认 100）
个任务，而 Redis 队列用的是 `zpopmin` —— **取走即删**。节点被 `SIGKILL`（`kill -9`）
硬杀时没有任何机会推回，这批任务就永久丢失了。

!!! note "实测"
    24 个任务的场景下，节点被硬杀会丢掉 20 个；优雅停止则全数恢复、且无重复。
    `docker stop`、Kubernetes 驱逐、`systemctl stop` 发的都是 `SIGTERM`，
    都会走优雅停止这条路。只有 `kill -9` 和 OOM Killer 绕不过去。

要缩小硬杀的损失面，可以调小 `COLLECTOR_TASK_COUNT`（代价是更频繁地访问 Redis）。
