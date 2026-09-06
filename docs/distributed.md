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

## 重新注入种子

想让爬虫重新从头跑（比如换了一批种子）：删掉种子锁和队列。

```bash
redis-cli DEL mineworker:NewsSpider:lock:seed mineworker:NewsSpider:z_requests
redis-cli DEL mineworker:NewsSpider:dedup:bloom      # 顺便清去重
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
