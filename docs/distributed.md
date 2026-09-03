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
