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

### 2. 真正的瓶颈是每请求新建 `httpx.Client`，不是线程模型

同样 32 线程，开启 `use_session` 后吞吐 **97 → 368 QPS（3.8 倍）**。
而默认情况下每个请求都新建一个 Client：`Request.use_session` 默认 `None`。

!!! warning "这一节原来的标题是「每请求重建连接」，这个归因翻过三次面"
    1. 最早写「每请求重建连接」；
    2. §5(a) 查明这里 3.8× 的真凶是 **`SSLContext` 构造**（32.9ms/个），
       不是重新建立 TCP 连接 —— **这一条至今成立**，它说的是
       `ssl_context_for` 缓存**之前**的那笔账；
    3. ~~后来给「按代理缓存 client」量收益时改口说「也不是连接复用」~~ ——
       **那次改口是错的**：直连下把「每条连接值多少钱」当旋钮拧，收益单调
       跟着涨（见 `settings.md` 的「这个收益就是连接复用」）。
       不过那次的两组代理数字**为什么相等，至今没有解释**。

    两件事要分开：**SSLContext 缓存之前**，省掉的主要是构造（32.9ms）；
    **缓存之后**（构造只剩 0.44ms），省下的就是连接本身。
    同一个配置在两个时代由不同的机制主导 —— 这是三次翻面的根源。

!!! danger "`setting.USE_SESSION` 曾经是个死配置（已修）"
    评估当时它在 `setting.py` 里有定义、文档里写着「复用 httpx 连接」，
    但**框架代码从没读过它** —— 只有 `Request(use_session=True)` 生效，
    在配置里写 `USE_SESSION = True` 得到的是静默无效果。
    现已修复，见 `mineworker/network/downloader/__init__.py` 的 `_wants_session`。

### 3. 线程越多越慢

零延迟（纯框架开销）下：

| 线程数 | 4 | 16 | 32 | 64 | 128 |
|---|---:|---:|---:|---:|---:|
| QPS | **1,277** | 700 | 529 | 525 | 503 |

超过 ~4 个线程后吞吐**单调下降**。所以原文那句「调大 `SPIDER_THREAD_COUNT`（开到 ~100 无妨）」
是错的：开到 100 反而更慢。

!!! note "这条曲线后来查明了归因：撞的是 httpx 自己的每请求 Python 开销"
    固定 32 线程逐层剥（零延迟本机靶子，每层只比上一层多加一样东西）：

    | 层 | QPS |
    |---|---:|
    | 裸 socket（复用连接） | 15,036 |
    | 换成一个**共享**的 `httpx.Client` | **534** |
    | 再换成每请求新建 client | 486 |
    | 再套上框架的下载器 | 487 |

    **一层就掉 28 倍，而框架在其上总共只加了约 10%。** 这个平台期是 GIL 串行的
    ——httpx 的请求路径是纯 Python，每请求约 1.9ms 的串行 CPU。
    调 `sys.setswitchinterval` 在 1000 倍范围内扫过没有作用（0.93~1.01×），
    不是车队效应。**这堵墙靠加线程翻不过去，只能加进程，或者换 `curl_cffi`**
    （见 `settings.md`）。

### 4. 公平比较下，异步下载器没有优势

50ms 延迟、**两边都开 session**（当年的数字）：

| 线程数 | sync | async |
|---:|---:|---:|
| 32 | **433** | 99 |
| 128 | **555** | 55 |

`AsyncHttpxDownloader` 早先看起来快，只是因为它内部天然共享 `AsyncClient`，
而对照组的同步下载器没开 session。一旦公平比较，同步更快。

!!! note "差距已大幅缩小（v4.34 之后复量，5 轮，均开 session）"
    | 线程 | 8 | 16 | 32 | 64 | 128 |
    |---|---:|---:|---:|---:|---:|
    | sync | 147 | 289 | **550** | **490** | **432** |
    | async | 143 | 286 | 464 | 234 | 213 |
    | async/sync | 0.97× | 0.99× | 0.84× | 0.48× | 0.49× |

    **方向没变，倍数变了**：当年是「同步快 4–10 倍」，现在 ≤16 线程基本持平、
    32 线程同步快 1.19×、64 线程往上快 2 倍。异步这一侧的提升来自
    [事件循环分片](settings.md#异步下载器的事件循环分片)（单个事件循环在 >24 线程时会坍塌）。

!!! danger "别用「同步不开 session」当对照 —— 那是这一节当年就点名过的坑"
    异步下载器**内部总是共享 client**，拿它去比「同步 + 每请求新建 client」
    等于在比两件不同的事。同一套复量，同步侧不开 session 时：

    | 线程 | 8 | 16 | 32 | 64 | 128 |
    |---|---:|---:|---:|---:|---:|
    | async/sync | 1.23× | 1.48× | **1.70×** | 0.61× | 0.50× |

    看起来异步在 ≤32 线程「有优势」—— **那是对照组被削弱了**。
    （v4.34 的记录里就写过这句错话，已一并更正。）

### 5. 后续：修复与复测（2026-09，同日）

上面的发现直接导出两个修复，都已落地：

**(a) 缓存 SSL context** —— `httpx.Client()` 每次构造都会新建 `SSLContext`（加载 CA 包），
实测 **32.9ms/个**；传入缓存的 context 后降到 **0.4ms**。而下载器默认每请求新建一个 Client，
于是这 33ms 是每请求的固定开销 —— 框架最大的单项成本。
缓存 `SSLContext` 与「共享 Client」不同：前者是无状态配置对象，**cookie 仍然每请求隔离，
抓取语义零变化**。

**(b) 接通 `USE_SESSION`** —— 见上文，它此前是死配置。

复测（50ms 延迟，**默认配置**，未开 session）：

| 线程数 | 4 | 16 | 32 | 64 | 128 |
|---|---:|---:|---:|---:|---:|
| 修复前 QPS | 40 | — | 97 | — | — |
| **修复后 QPS** | **67** | **204** | **308** | **435** | **555** |
| 效率 | 84% | 64% | 48% | 34% | 22% |

- **默认配置下 32 线程提升 3.2×**（97 → 308），无需用户改任何配置
- **「线程越多越慢」消失了**：修复前吞吐在 ~100 QPS 封顶且零延迟下单调下降，
  现在随线程数单调增长。那 33ms 的 CA 解析本身就是争用源（占着 GIL）
- 效率随线程数下降属正常收益递减：128 线程的 555 QPS 已逼近零延迟测得的框架
  纯开销上限（~674 QPS）

**所以剩下的天花板是单进程每请求的 CPU 成本（GIL），不是某把可优化的锁。**
再往上要靠多进程 —— 而分布式 [`Spider`](distributed.md) 本来就提供了这条路：
起多个 worker 进程共享 Redis 队列，比在单进程里堆线程有效得多。

线程数怎么选：延迟 50ms 时 4 线程效率 84%、16 线程 64%、128 线程 22%。
**盲目调大只会拉低单线程效率**，且对目标站不礼貌。先看目标站能承受多少，再定这个数。

### 结论：async 批量分发不做

要突破的那个「天花板」根本不是瓶颈 —— 在触及它之前，每请求建连和线程争用早就先撞墙了。
把 worker 改成批量分发（本项目风险最高的改动）解决不了任何一个实测到的问题。

**该做的是别的**（已进 Roadmap）：让 `USE_SESSION` 真正生效、把默认线程数调回小值、
定位零延迟下线程增加导致吞吐下降的争用点。

---

原先的建议（**已被上面的实测推翻，保留作对照**）：
~~调大 `SPIDER_THREAD_COUNT`（开到 ~100 无妨）、开 `HTTPX_HTTP2`、开 `DOWNLOADER_ASYNC`
拿连接复用，通常就够了。~~
