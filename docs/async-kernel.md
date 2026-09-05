# async 内核评估

> Roadmap 里「async 内核 —— 评估用 async httpx 替换线程模型」的结论。
> **结论：不做全量 async 重写；落地一个隔离的异步下载器 `AsyncHttpxDownloader` 作为可选加速项。**

## 现在的线程模型

```
主线程            run(): 注入种子 → 起线程 → _wait_until_done 轮询 → teardown
N×ParserWorker    collector.get → 中间件 → download_request(阻塞 httpx) → validate → parse → 分发
RequestBuffer     线程，周期 flush → 去重 → 任务队列
ItemBuffer        线程，周期 flush → 去重 → pipeline.save_items(阻塞 pymongo / pymysql)
_Heartbeat        线程，周期 hset(阻塞 redis)      —— 仅 Spider
_TaskPoller       线程，周期 lpop(阻塞 redis)      —— 仅 TaskSpider
_RenderPool       pool_size×线程，各持一个 sync chromium
```

并发靠 `SPIDER_THREAD_COUNT` 个工作线程。socket 等待时 GIL 释放，lxml 解析在 C 扩展里
也释放 GIL，所以线程模型对 I/O 密集抓取是够用的——feapder 本身就这么跑生产。

## async 能买到什么 / 买不到什么

| | 说明 |
|---|---|
| ✅ 单线程驱动上千并发连接 | 每连接开销更低、FD 更省。仅在「大扇出 + 解析极轻 + 目标站不限速」时真正兑现 |
| ✅ `redis.asyncio` / `httpx.AsyncClient` / playwright async API | 都现成 |
| ⚠️ 解析是 CPU-bound | 单事件循环会把所有 `parse` 串行化，最终还得甩进 executor → 又回到线程 |
| ❌ 落库 | `pymongo` / `pymysql` 没有干净的 async 路径（要换 `motor` / `aiomysql`）。而 ItemBuffer 本来就批量写，不是瓶颈 |
| ❌ feapder 心智兼容 | 用户写同步 `def parse` + `yield` 是明确约束。async 内核要么逼用户写 `async def`，要么陷入混合模型 |

**混合模型是陷阱**：保留同步 `def parse` 丢进 executor 跑 → 事件循环**和**线程池同时存在，
只把「下载等待」挪出了线程，解析吞吐照旧，还多一套并发模型要 debug。Scrapy 能走 async 是
因为它整个 API 都是 async 原生的——那正是本项目排除掉的路。

## 重写的成本

几乎是整个 core + network + dedup + 一半测试：`base_scheduler`（TaskGroup / `asyncio.Queue` /
信号）、`parser_control`、`collector`、两个 buffer、`task_queue`、`_httpx`、`downloader/base`
（连带 `Request.download()` 和 `mineworker shell` 的同步入口）、`redis_scheduler` /
`redis_task_scheduler` / `redis_filter`、`proxy_pool`、`user_pool/redis`，以及
`test_air_spider` / `test_spider` / `test_task_spider` / `test_integration_httpserver` /
`test_downloader` / `test_render` / `test_spider_persistence` 全部重写（现有测试深度依赖
「`start()` 阻塞 + 线程不泄漏 + `threading.Timer` 停止」）。这是 v3 级别的工作量。

投入产出比不成立：收益只在少数派工作负载上兑现，而那类负载通常先撞上目标站限速。

## 落地：`AsyncHttpxDownloader`

作为评估的产出，做了一个**隔离的异步下载器**，捕获大部分收益而 API 零改动：

- 一个专属事件循环线程 + 一个共享 `httpx.AsyncClient` 承载所有在途连接
- 对外仍是同步 `Downloader.download()`：工作线程提交协程到内部 loop 并阻塞等结果（和渲染池同套路）
- 连接池 / keep-alive / HTTP/2 多路复用被**所有 worker 共享**（同步下载器在 `use_session=False`
  时每请求新建 client，没有 keep-alive）
- `DOWNLOADER_ASYNC_CONCURRENCY` 信号量 + 连接池上限双重限流

```python
# setting.py
DOWNLOADER_ASYNC = True             # 普通请求走 AsyncHttpxDownloader
DOWNLOADER_ASYNC_CONCURRENCY = 200  # 最大在途请求数
HTTPX_HTTP2 = True                  # 需 pip install "httpx[http2]"，同步 / 异步下载器都生效
```

`render=True` 的请求不受影响（仍走渲染池）。

### 目前的天花板

工作线程是「1 线程 : 1 在途请求」，所以真正在途的请求数仍 ≈ `SPIDER_THREAD_COUNT`。
`AsyncHttpxDownloader` 现在的收益是**连接复用 + HTTP/2 + 更低 FD**，不是「少量线程跑上千并发」。

要突破这个天花板，需要 worker 侧批量分发：少量 worker，每个从 collector 取一批、
`await asyncio.gather(*downloads)` 拿到全部响应后再逐个 `parse`。这会改动 `parser_control`
和结束检测，属于「真需要时再做」——目前没有实测证据表明线程调度是瓶颈。

## 何时重新评估

- 实测某目标：在途连接数远小于期望、且 CPU / 带宽 / 目标站限速都不是瓶颈
- 需要单机十万级并发连接（此时线程栈内存和上下文切换才真正咬人）

## 实测（2026-09）

上面「何时重新评估」的条件触发了 —— 于是建了 [`benchmarks/`](https://github.com/apersonw/mineworker/tree/main/benchmarks)
去拿数据。**结果推翻了这一页原本的几处推断。**

靶子是本地 asyncio 服务（自检可扛 512 并发 / 7,300 QPS，远高于被测），
QPS 用服务端计时，并发取**时间加权平均**而非峰值。

### 1. 「1 线程 1 在途」是个从未达到的上限

| 线程数 | session | QPS | 理论 QPS | 效率 | 平均在途 |
|---:|:-:|---:|---:|---:|---:|
| 4 | ✗ | 40 | 80 | 50% | 2.0 |
| 4 | ✓ | 73 | 80 | 92% | 3.8 |
| 32 | ✗ | 97 | 640 | 15% | 11.6 |
| 32 | ✓ | **368** | 640 | 58% | 20.6 |

峰值并发确实能摸到线程数，但**时间加权平均只有它的 15%–58%**。
线程大部分时间并不在等网络。

### 2. 真正的瓶颈是每请求重建连接，不是线程模型

同样 32 线程，开启连接复用后吞吐 **97 → 368 QPS（3.8 倍）**。
而默认情况下连接**不复用**：`Request.use_session` 默认 `None`。

!!! danger "`setting.USE_SESSION` 是个死配置"
    它在 `setting.py` 里有定义、在文档里写着「复用 httpx 连接」，
    但**框架代码从没读过它**——只有 `Request(use_session=True)` 生效。
    在配置里写 `USE_SESSION = True` 得到的是静默无效果。

### 3. 线程越多越慢

零延迟（纯框架开销）下：

| 线程数 | 4 | 16 | 32 | 64 | 128 |
|---|---:|---:|---:|---:|---:|
| QPS | **1,277** | 700 | 529 | 525 | 503 |

超过 ~4 个线程后吞吐**单调下降**。所以原文那句「调大 `SPIDER_THREAD_COUNT`（开到 ~100 无妨）」
是错的：开到 100 反而更慢。

### 4. 异步下载器在连接复用后是拖累

50ms 延迟、均开 session：

| 线程数 | sync | async |
|---:|---:|---:|
| 32 | **433** | 99 |
| 128 | **555** | 55 |

`AsyncHttpxDownloader` 早先看起来快，只是因为它内部天然共享 `AsyncClient`（即连接复用），
而对照组的同步下载器没开 session。**一旦公平比较，同步反而快 4–10 倍**，且线程越多差距越大。

### 结论：async 批量分发不做

要突破的那个「天花板」根本不是瓶颈 —— 在触及它之前，每请求建连和线程争用早就先撞墙了。
把 worker 改成批量分发（本项目风险最高的改动）解决不了任何一个实测到的问题。

**该做的是别的**（已进 Roadmap）：让 `USE_SESSION` 真正生效、把默认线程数调回小值、
定位零延迟下线程增加导致吞吐下降的争用点。

---

原先的建议（**已被上面的实测推翻，保留作对照**）：
~~调大 `SPIDER_THREAD_COUNT`（开到 ~100 无妨）、开 `HTTPX_HTTP2`、开 `DOWNLOADER_ASYNC`
拿连接复用，通常就够了。~~
