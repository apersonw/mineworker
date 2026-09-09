# Changelog

本文件格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 修复

- **三个下载器在开代理池时都不复用连接。** `_client_for()` 的复用条件是
  `proxy == self._proxy` —— 拿**配置**（下载器的固定代理，通常 `None`）
  去比**状态**（这次实际使用的代理）。开代理池时两者永远不等，
  于是每个请求都新建一次 client：一轮全新的 TLS 握手 + 代理隧道。

    实测（5 个请求、池里只有一个代理，最有利于复用的情形）：
    不开代理池建 **1** 个 client，开代理池建 **5** 个。
    而压测结论正是「瓶颈是每请求建连，不是线程模型」——
    这个开关在开代理池的部署里等于没有，而代理池是生产环境的常态。

    `curl` 用的是**一模一样**的判据；`async` 写法不同（有代理就每请求建一个
    一次性 client）但后果相同 —— 实测同样是 5 个请求建 5 个。

    现在按「实际使用的代理」缓存连接池，同一个代理的连续请求复用同一个。
    这套逻辑抽成了共用的 `ProxyClientCache`，三个下载器一起用 ——
    散成三份的话，下次改还是只会改到一个。

### 新增

- `SESSION_CACHE_SIZE`（默认 16）—— 每代理连接池的缓存上限。
  代理池可能有上千个代理，不设上限就是把性能问题换成资源泄漏；
  超出上限时最久没用的会被关掉。

### 修复

- **关闭时一步失败，后面的收尾动作全被跳过。** `_teardown()` 是一串无保护的
  串行调用，而最关键的两步排在后面：把内存里的数据落库、把持有的任务推回队列
  （分布式下还要释放租约）。

    关机时 Redis 抖一下，第一步「推送剩余请求」就抛 —— 实测**数据没落库、
    任务也没推回**。最可能抛的那一步，正好排在它们前面。

    现在每一步独立兜住，失败只记日志、不影响后面。顺序保持不变：
    落库仍排在任务推回之前（销账依赖它，否则会重复推送）。

### 修复

- **Item 去重的 Redis 调用抖一下，这一批剩下的数据会凭空消失。**
  `ItemBuffer._persist()` 里有两处去重调用走 Redis：归一化时的查重、
  写成功后的记指纹。任一抛出，异常就从落库循环里穿出去，
  后面的分组既没写库、也没 dump。

    实测（9 条数据分 3 组）：查重抖一下 **整批 9 条全丢**；
    记指纹抖一下 **第一组写成功、剩下 6 条消失**。分布式下「不销账」会让任务
    被重抓，但 `AirSpider` 没有租约 —— 就是永久丢失。

    现在去重不可用时按「没见过」放行并继续写库（去重是优化，丢数据不是可选项），
    记一条 warning，并新增 `dedup_degraded` 计数；运行汇总里会带上
    「⚠️ 去重降级 N 批（可能重复入库）」—— 静默降级只是把一种无声换成另一种。

### 修复

- **写去重指纹时 Redis 抖一下，缓冲区里剩下的请求会静默消失。**
  `RequestBuffer.flush()` 的循环里挨着两次 Redis 调用：`dedup.add()`（写指纹）
  和 `queue.put()`（入队）。0.10.2 修好了 `put` 失败不丢请求，
  `dedup.add` 却留在 try 之外。

    实测（10 个请求、第 3 个上抖一次）：抖在 `put` 丢 0 个，
    抖在 `dedup.add` **丢 8 个** —— 既不在队列、也不在缓冲区、也没落盘。

- **入队失败放回缓冲区时，没查过重的请求会绕过去重。** 放回逻辑把整个尾巴
  一律清掉 `filter_repeat`，但其中只有第一条真的过了去重（指纹已写、`put` 才失败），
  后面那些根本没走到去重那一步。

    实测：批次里两条相同 URL、`put` 抖一次之后，**同一个 URL 进队列 2 次**。
    现在只对确实写过指纹的那一条清标志。

## [0.14.0] - 2026-09-09

### 修复

- **`retry --requests` 会删掉失败记录，却什么都没恢复。** 它只是把请求重新下载一遍
  看状态码 —— **不跑回调、不产出 item、不入库** —— 然后把记录从文件里删掉，
  日志还说「恢复 N」。

    实测（站点 503 → 请求失败落盘 → 站点恢复 → 回放）：报告「恢复 1」，
    **库里 0 行，文件已删除**。函数自己的 docstring 写的是「用于确认目标是否恢复」，
    是文档（代理池 / 账号池 / 渲染三处）把它说成了数据恢复。

    现在它就叫探活：输出改成「N 条已可达」，**记录一条不删**。

### 新增

- `RETRY_FAILED_ON_START` —— 爬虫启动时把 `failed_requests.jsonl` 重新灌回队列，
  走完整的下载 → 回调 → 落库。这才是把数据抓回来的那条路。
  回放的请求会清掉 `filter_repeat`：这些 URL 的指纹已经在去重里，
  不清就会被静默挡下，变成「回放跑了但什么都没发生」。

## [0.13.3] - 2026-09-09

### 修复

- **批次任务标「已完成」时，数据还只在内存缓冲里。** 文档推荐的写法是
  `yield {...}` 之后紧接着 `self.update_task(task["id"], ok=True)` ——
  那一刻数据还没落库，而任务表已经写成完成。节点这时一死：数据没了，
  任务也回不来（`reset_lost_tasks` **只回收「处理中」**）。

    真 MySQL 实测：5 个任务全标完成，实际落库 **0 行**，批次报告 100% 完成。
    这是 0.11.1 那次「销账早于落库」在**批次任务表**上的化身 ——
    那次修的是请求队列的销账，批次状态是另一个状态存储，同一个洞原样还在。

    现在 `update_task(ok=True)` 会等这次请求产出的数据整批落到持久介质之后才写任务表。
    仍立即写入的三种情况：没产出 item、`ok=False`、不在请求处理过程中调用。
    用户代码不用改 —— 照旧文档写的爬虫自动变对。

## [0.13.2] - 2026-09-09

### 修复

- **中间件发起的重试不受任何约束；0.13.0 起会打成死循环。**
  `process_response` 返回 Request（账号池的「掉登录，换号重试」走的正是这条）时，
  框架**既不递增 `retry_times` 也不走重试路径**，而中间件还会清掉 `filter_repeat` ——
  `SPIDER_MAX_RETRY_TIMES` 管不住它，去重也拦不住它。

    实测（1 个页面、`SPIDER_MAX_RETRY_TIMES=3`、账号池空 + 允许匿名、
    站点一直回登录墙）：**目标站被打了 76 次 / 8 秒**，只被 `SPIDER_MAX_RUNTIME` 拦住。

    机制本身一直没有预算，但 0.13.0 之前有一个**偶然的**终止条件：每次重试都会
    拉黑一个账号，池子迟早空掉，那时旧代码直接放行响应。0.13.0 让「没挂账号也跑
    `check_login`」，把那个终止条件拿掉了。**用了账号池并开着
    `USER_POOL_ALLOW_ANONYMOUS` 的人应尽快升级。**

    现在这条路径计入重试预算，超限即判失败落盘；计数也从 `REQUEST_OK` 改成 `RETRY`
    （汇总里的「请求成功」会相应变小 —— 一次「被判为无效、要重来」的响应本来就不是成功）。
    修复后同场景 **4 次**，正好是配置的上限。

## [0.13.1] - 2026-09-08

### 修复

- **停止渲染池时，队列里排着的请求会让调用线程永久挂起。** `submit()` 的
  `job.event.wait()` 没有超时，而 `close()` 只给 worker 置停止位、塞一个哨兵，
  **不排空队列** —— worker 一看见停止位就退出，剩下的任务永远不会被收尾。

    实测（1 个在渲染、4 个排队时关闭）：**4 个调用线程永久挂起**。
    这直接打在优雅停止上：worker 卡在 `event.wait()` 里，`stop()` 它不看、
    `join()` 它不动，而 `close_default_downloaders()` 还排在 worker join 之后。

    现在 `close()` 会排空队列并把排队中的任务收尾成失败（调用方拿到 `RequestError`，
    重试用尽后落进 `failed_requests.jsonl`）；`submit()` 的等待也加了上限作为兜底，
    防「worker 不在了却没人收尾」。修复后同场景挂起 **0** 个。

## [0.13.0] - 2026-09-08

### 修复

- **账号池空了之后，登录墙页面被当成数据存进了库。** 取不到账号时中间件只记一条
  warning 就放行，请求照发、没有 cookie；而 `process_response` 第一行是
  `if user is None: return response` —— **用户写的 `check_login` 一次都没被调用**。

    实测（靶子在账号用满 3 次后封号，池里就一个号）：**12 行落库里 10 行是登录墙**，
    爬虫还正常报告跑完了。比代理那次更糟 —— 不只是请求发错了，是数据被污染了。

    现在池空时先等 `USER_POOL_WAIT_TIMEOUT`（新增，默认 10 秒），仍拿不到就抛
    `UserUnavailableError`（新增）走重试，不匿名发出；`check_login` 改为判断响应本身，
    与有没有挂账号无关。该错误不计入熔断。修复后登录墙 **0** 行、真实数据不受影响。

    ⚠️ 行为变化：定义了 `user_pool()` 的爬虫，账号耗尽时从「匿名继续抓」变成
    「报错重试」。想要「有号更好、没号也能抓」的，把新增的
    `USER_POOL_ALLOW_ANONYMOUS` 设为 `True`。

- **单机模式下重试耗尽的请求会凭空消失。** `_on_failed_request` 是空实现 ——
  分布式模式一直把这些请求推进 Redis 失败列表，单机这边漏了，
  而 `failed_requests.jsonl` 的既定用途正是装它们（`retry --requests` 就读这个文件）。
  现在单机也会落盘。

## [0.12.0] - 2026-09-08

### 修复

- **代理池耗尽后静默直连，源 IP 就那样暴露给目标站。** `pick_proxy()` 取不到代理时
  返回 `None`，而下载器只在拿到代理时才设置代理参数 —— 请求直接从本机 IP 打出去，
  没有日志、没有计数、没有报错。

    实测（`PROXY_ENABLE=True`、`PROXY_MAX_USE_TIMES=5`，取号接口中途断供）：
    **21 个请求里 16 个走了直连**。而开代理池的全部意义就是别这么干 ——
    目标站看到的是真实 IP，该被 ban 的是它。

    现在拿不到代理时先等 `PROXY_WAIT_TIMEOUT`（新增，默认 30 秒）并按
    `PROXY_MIN_INTERVAL` 重新取号，仍拿不到才抛 `ProxyUnavailableError`（新增），
    走正常重试。该错误不计入熔断 —— 代理供应是自己这边的问题，
    算进去的话代理商断供五分钟就能把所有域全熔断一遍。修复后同场景直连 **0** 个。

    ⚠️ 行为变化：老配置从「悄悄直连」变成「报错重试」。确实想要
    「有代理就用、没有就直连」的，把新增的 `PROXY_ALLOW_DIRECT` 设为 `True`。

- 异步下载器改用 `await` 版的代理获取。阻塞式等待会把整个事件循环卡住，
  其它并发请求跟着一起停。

## [0.11.3] - 2026-09-08

### 修复

- **`UpdateItem` 指向不存在的行时，SQL 管道报告成功而数据凭空消失。**
  裸 `UPDATE` 匹配不到行不报错、只影响 0 行，而 `update_items` 忽略了这个数字。
  真库实测：`update_items` 返回 `True`、表里 0 行、不 dump、
  **去重指纹照记（这条 URL 从此不会再被抓）**、统计里算成功 1 条。

    最扎眼的是首次运行：照着 Mongo（`update_one` upsert）/ ES（`doc_as_upsert`）
    的语义写的 `UpdateItem` 爬虫，换到 SQL 上第一跑什么都没写进去，还报告成功。

    现在没匹配到任何行算写入失败：整批 dump（可用 `retry --items` 回放），
    日志点名具体的键，指纹不记。SQL 的 `UpdateItem` 仍是 `UPDATE` 而非 upsert ——
    想要 upsert 用 `MYSQL_UPDATE_ON_DUPLICATE` 或 `POSTGRES_ON_CONFLICT="update"`。

- MySQL 连接加上 `CLIENT.FOUND_ROWS`，`rowcount` 改为按「匹配到几行」计数，
  与 PostgreSQL 一致。默认口径下值没变的 `UPDATE` 返回 0，和「这行不存在」分不开 ——
  上一条会因此把正常的幂等重写反复判成失败。`MysqlDB.execute()` 的返回值语义随之改变。

## [0.11.2] - 2026-09-08

### 修复

- **写库失败的 `UpdateItem` 经 `retry --items` 回放后，那次更新会被静默丢弃 ——
  而 retry 报告成功并删掉了文件。** `_dump_failed` 只记 `table` + `data`，
  `update_keys` 和逐条的 `pipelines` 路由都丢了；`retry_items` 于是无条件走
  `save_items`，即 INSERT 而非 UPSERT；PostgreSQL 默认的 `ON CONFLICT DO NOTHING`
  让这条 INSERT **什么都不做并返回成功**；retry 据此报告「成功 1，仍失败 0」、
  删除文件 —— 那是最后一份副本。

    真库实测：回放后库里仍是 `旧标题/1`，而期望是 `新标题/99`。
    每一环都按自己的契约正确工作，合起来是静默、永久、还报告成功的丢失。
    0.11.1 刚把「dump 也算持久介质，可以销账」写成保证，这条退路本身却是有损的。

    现在 dump 带上 `update_keys` 与 `pipelines`，回放按记录路由；
    没有这些字段的旧文件按普通插入处理。管道没实现 `update_items` 时那一组算失败
    留在文件里，不会让整个回放崩掉。

## [0.11.1] - 2026-09-08

### 修复

- **节点被硬杀会静默丢数据 —— 而且重跑也补不回来。** 销账发生在解析完成那一刻，
  那时 item **还只在内存缓冲里**。销账等于释放租约、宣布「这条干完了」，于是节点一死：
  数据没了（只在内存里）、任务不会被回收（已销账）、**重跑还是跳过**（请求指纹是入队前写的）。

    实测（400 页、跑到一半 SIGKILL、第二个节点接手跑完）：

    | 管道每批写入耗时 | 落库行数 | 静默丢失 |
    |---|---|---|
    | 0.3 秒 | 388 / 400 | 12 |
    | 1.5 秒（≈ MySQL 批插） | 340 / 400 | **60** |

    三次运行都是靶子确实发出全部 400 页、爬虫 **exitcode=0 报告抓取完成**。
    框架对**任务**给的是「至少一次」，对**数据**实际给的是「至多一次」——
    而两者从外部看一模一样。此前全部分布式用例都写着 `ITEM_PIPELINES = []`，
    数据这条路从没被验过。

    现在产出过数据的请求，销账权交给 `ItemBuffer`：整批落到持久介质
    （入库成功，或写库失败 dump 进 `failed_items`）之后才销账。修复后两种场景都是
    **400 / 400**。代价是重抓从 6 页涨到 23 / 66 页 —— 把「静默丢 60 条」
    换成「多抓 60 页」。数据这边现在是**至少一次**，`ITEM_FILTER_ENABLE` 该开着。

- `ItemBuffer.flush()` 里落库抛穿会把整个 buffer 线程带走，此后再没有任何数据落库，
  且一声不响。现在捕获并记日志，且**不销账** —— 任务留给租约回收，数据还有第二次机会。

- **健康节点会被误判为「已死」，任务被别的节点抢走重抓。** 0.11.0 的任务租约是
  **从领走那刻起算的**，而 collector 一次领 `COLLECTOR_TASK_COUNT`（默认 100）个 ——
  每页处理慢一点，排在后面的任务在被碰到之前租约就过期了。

    实测（5 个任务、每个处理 0.6 秒、租约 2 秒）：节点全程健康、一直在干活，
    仍被抢走 **2 / 5** 个。后果不是丢数据（去重挡得住重复入库），而是**重复抓取** ——
    目标站挨双倍流量，正好抵消框架主打的礼貌性。

    现在心跳线程每 `HEARTBEAT_INTERVAL` 顺手续本节点持有的任务租约
    （缓冲区排队的 + worker 正在处理的都算）。续期用 `ZADD XX` 只更新已存在的成员：
    已销账或已被别人回收的任务不会被塞回在途表，否则两个节点会同时成为持有者。

    修复后同场景被抢走 **0 个**，而不续期的节点（被 SIGKILL）仍能正常回收 ——
    两条同时成立，0.11.0 的硬杀恢复没有被削弱。

## [0.11.0] - 2026-09-08

三件事：硬杀场景下的任务恢复、开发期响应缓存、钉钉 / 企业微信告警。
**跑分布式的用户建议升级** —— 在此之前节点被 OOM Killer 杀掉，它领走的任务就没了。

### 新增

- **任务租约 `SPIDER_TASK_LEASE`（默认 600 秒）—— 节点被硬杀后任务不再永久丢失。**

    0.8.1 的优雅停止与 0.10.3 的退出落盘都要求**进程还活着**，而 OOM Killer、断电、
    `docker kill` 不给这个机会 —— 这是它们结构上覆盖不到的场景。队列用 `zpopmin`
    取走即删，任务进了某个节点的内存后 Redis 里就不存在了。

    实测（24 个任务，节点跑 1.2 秒后 `SIGKILL`，再起一个节点续爬）：
    **修复前只剩 5 个，修复后全部恢复**。

    做法：领走任务的同时把它记进在途表（与 `zpopmin` 用 Lua 绑成原子 —— 分两步做
    会出现「取走了但没记账」，那比现在丢得更隐蔽），处理完销账；租约到期的任务由
    任意节点放回队列。结束检测也会等在途任务，不再在队列空了就收工。

    ⚠️ **这带来「至少一次」语义**：节点只是卡住而非死了的话，任务会被处理两遍。
    请求去重能挡住大部分重复入库，但回调仍可能跑两次，有副作用的操作要自己保证幂等。
    设 `SPIDER_TASK_LEASE = 0` 可回到原来的行为。

### 新增

- **钉钉与企业微信告警渠道**（`WARNING_DINGTALK_WEBHOOK` / `WARNING_WECHAT_WEBHOOK`）。
  钉钉支持「加签」模式（`WARNING_DINGTALK_SECRET`）；用「自定义关键词」模式的话
  消息标题固定带 `【MineWorker】`，把关键词设成 `MineWorker` 即可。

### 修复

- **告警发送失败会被静默吞掉。** 飞书 / 钉钉 / 企业微信在 webhook 失效、关键词
  不匹配、需要加签这些情况下**都返回 HTTP 200**，真正的结果在响应体的错误码里；
  而 `httpx.post` 对 4xx/5xx 也不抛异常。原来只捕获 `HTTPError`，等于什么都没检查 ——
  **你以为告警发出去了，其实一条都没到**。

    告警系统静默失效是最坏的一种：出事那天才发现它自己早就哑了。
    现在状态码和响应体都会检查，被拒绝时记 ERROR 日志并带上对方的错误码。

### 新增

- **响应缓存 `RESPONSE_CACHE_ENABLE`（默认关闭）。** 写爬虫是来回试的过程：改一版
  选择器、重跑一遍，一天下来同一批页面可能被抓几十遍 —— 慢，且对目标站不礼貌。
  打开后第一遍照常抓，之后重跑直接读本地文件。

    **命中缓存时不发请求、也不占限速名额**（钩子挂在 `download_request()` 里、
    限速之前），否则「重跑不打扰目标站」只做了一半。

    不缓存两类东西：**非 GET**（不幂等，重放等于把一次写操作的结果当成读结果）、
    **状态码不正常的响应**（把 429 存下来，等于每次重跑都在读那张限速页）。
    读写失败都只记 debug 然后走真实请求 —— 加速手段不该变成新的故障点。

    配套 `mineworker cache` / `mineworker cache --clear`。用文件而非 Redis：
    主要场景是本机调 `AirSpider`，那时未必有 Redis 在跑。

    这是对标 feapder 时找出的缺口 —— 它有 `RESPONSE_CACHED_ENABLE` 而我们没有，
    且这项与框架自身主张的礼貌性取向一致，缺它说不过去。

## [0.10.4] - 2026-09-08

分布式多节点的并行度修复。**用多个节点跑同一个爬虫的用户建议升级** ——
在此之前很可能只有一个节点在真正干活。

### 修复

- **多节点同时启动时，除播种节点外的其余节点会立刻空跑退出。** 只有一个节点能拿到
  种子锁，其余节点看到的是空队列 —— 而默认 `DONE_CHECK_TIMES=3 ×
  DONE_CHECK_INTERVAL=0.5s` = 1.5 秒就判定「抓完了」，播种节点那时还没把种子推进队列。

    `_all_nodes_idle()` 挡不住：播种节点在那一刻的 `pending` 同样是 0，
    它自己还没开始拉活，看上去和「闲着」没区别。

    **后果是配 N 个节点、实际只有 1 个在干活，并行度静默归零，没有任何报错。**
    这是在 MineWorkerHub 上跑真实任务撞出来的：两个 worker 容器同秒启动，
    一个抓完 63 个请求，另一个 1 秒后退出、0 个请求。

    新增 `SPIDER_STARTUP_GRACE`（默认 10 秒）：节点在**一个任务都没见过**之前
    不判定结束。拿到过活之后完全按原规则走，不拖慢正常结束；代价只在真的没活干时
    体现（多等 10 秒才退出）。修复后同样两个节点：35 + 28 = 63 请求，交集 0。

## [0.10.3] - 2026-09-07

退出时的数据完整性。**补上 0.10.2 兜底说法的漏洞** —— 那次声称永久性故障由
`DUMP_UNFINISHED_ON_EXIT` 兜底，但那条路径恰好不会触发。

### 修复

- **非中断退出时不 dump 未完成请求。** `AirScheduler._on_shutdown` 的 dump 卡在
  `self._interrupted` 上，而 `SPIDER_MAX_RUNTIME` 走的是**不设**这个标志的路径
  （它只服务于「再按一次 Ctrl-C 强制退出」）—— 于是超时停止时缓冲区 / 队列里的
  存货全部丢失。实测：5 条存货、非中断退出 → dump 文件里 **0 条**。

    这也让 0.10.2 的兜底说法落了空：那次说「永久性故障仍由既有的
    `DUMP_UNFINISHED_ON_EXIT` 在退出时兜底」，但 Redis 永久故障时请求会一直留在
    缓冲区、爬虫永远不算完成、最后由 `SPIDER_MAX_RUNTIME` 停止 —— 恰好是不 dump
    的那条路。现在只要还有存货就 dump，开关仍是 `DUMP_UNFINISHED_ON_EXIT`；
    正常跑完的爬虫没有存货，不会平白多出文件。

- **分布式节点退出时推回 Redis 失败没有兜底。** `RedisScheduler._on_shutdown`
  逐个把存货推回队列，**而它恰恰是在「Redis 出问题」的场景下被调用的** ——
  一旦 put 抛异常，剩下的请求既不在队列里、也不在缓冲区里，静默消失。
  现在推不回去就落盘到 `FAILED_REQUEST_PATH`，等 `mineworker retry` 回放。
  （和 0.10.2 修的 `flush` 是同一个形状，只是换了个地方。）

## [0.10.2] - 2026-09-07

请求缓冲的失败处理。**分布式部署的用户建议升级** —— Redis 一次抖动就会
静默丢掉一批请求，并让缓冲区线程永久停摆。

### 修复

- **队列一次抖动会静默丢掉一批请求，还会把缓冲区线程打死。**
  `RequestBuffer.flush()` 先把整批从 `_pending` 摘走再逐个推进队列，中间任何一次
  出错（Redis 断连 / OOM / 网络闪断），剩下的请求**既不在缓冲区、也没进队列、
  也没有像 Item 那样 dump 到文件** —— 静默消失。实测 10 个请求、第 3 个上抖一次：
  进队列 2 个、丢 8 个。

    而且 `run()` 里没有 try/except，后台 flush 线程会直接死掉，此后**所有**请求
    都不再入队，爬虫静默停止发现新页面（实测线程存活 `False`）。

    现在出错时把未入队的请求放回缓冲区，下一轮自动重试；后台线程兜住异常并记日志
    （永久性故障仍由既有的 `DUMP_UNFINISHED_ON_EXIT` 在退出时兜底）。

    **放回去还不够**：`dedup.add()` 是在入队**之前**写指纹的，重试时会撞上自己刚
    写下的那条指纹、被当成重复丢弃 —— 所以放回的请求要清掉 `filter_repeat`，
    它们已经过过去重了。反向验证：只去掉这一步，用例照样红。

    （对照 `ItemBuffer`：落库失败一直会记日志并 dump 到 `FAILED_ITEM_PATH`。
    又是同一份代码里的两半，一半想好了一半没有 —— 和 0.10.1 里的锁续期 / 释放一样。）

## [0.10.1] - 2026-09-07

`BatchSpider` 的并发正确性。**用 `BatchSpider` 的用户建议升级。**

### 修复

- **`BatchSpider` 的 master 互斥一旦超时一次就永久破掉。** `_renew_lock` 是无条件
  `SET`，不校验锁还是不是自己的：master A 的一次 tick 超过锁 TTL（60 秒）→ 锁过期 →
  master B 用 `SET NX` 合法拿到 → **A 下次续期把 key 覆盖回自己的 node_id，把锁
  抢了回来**。此后两个 master 互相覆盖，同时认领任务，同一批 URL 被抓两遍。

    现在续期与释放都是原子的「是我的才操作」（Lua 比对 node_id），
    续期发现锁易主就让本节点退出，不再认领任务。

    （释放路径本来就校验了持有者，只是那边也是非原子的 GET-then-DEL，一并收拾。）

- **`MysqlBatchStore.claim_tasks` 是非原子的「SELECT 然后 UPDATE」。**
  没有事务、没有 `FOR UPDATE`，多个认领者会 SELECT 到同一批行。真库实测
  （6 个并发认领者）：200 个任务被认领 1200 次，每个都被领了 6 遍。

    现在在一个事务里 `SELECT ... FOR UPDATE` + `UPDATE`。为此给 `MysqlDB` 加了
    `transaction()` —— 它此前是 autocommit 且每次调用各借一条连接，所以就算写了
    `FOR UPDATE` 也锁不住（两条语句在两个事务里，行锁早放掉了）。
    `MemoryBatchStore.claim_tasks` 同样补了锁。

    没有加「认领令牌」列：那是破坏性的表结构变更，修 bug 不该顺带要求所有人迁移数据。

    正常单 master 部署下这个竞态不会撞车（`claim_tasks` 只由持锁的 master 调用）——
    真正会触发它的是上面那条锁缺陷。但 `BatchStore.claim_tasks` 是公开接口，
    自己写 master 的用户直接暴露在其中。

- **没装 `[redis]` 时 `mineworker.db.mysqldb` 导不进来。** `mineworker/db/__init__.py`
  在顶层 `from ... redisdb import ...`，而 redisdb 顶层 `import redis` —— 于是装了
  `mineworker[mysql]` 但没装 redis 的用户，连 `MysqlDB` 都拿不到。
  导一个子模块不该把父包的可选依赖一起拖进来。改成惰性导出。

### 测试

- `MysqlBatchStore` 此前**从没碰过真数据库** —— 测的是 SQL 字符串形状，
  那只验证了「我写出了我想写的字符串」。现在接真 MySQL，并新增 master / worker
  **分进程**的端到端用例（真 MySQL 任务表 + 真 Redis 队列，判据取自 HTTP 靶子的
  命中次数）。这和 v3.0 给 `MysqlPipeline` 补真库测试是同一个缺口。

## [0.10.0] - 2026-09-07

去重的规模边界。**抓取量可能超过 100 万 URL 的用户建议尽快升级** ——
在此之前你可能一直在静默丢数据。

### 新增

- **布隆去重改为分层，不再在超容后静默失效。** 布隆此前是固定容量
  （`DEDUP_INITIAL_CAPACITY` 默认 100 万），超容后误判率急剧升高 —— 而在去重里
  「误判」意味着**一个从没抓过的 URL 被当成「已抓过」静默丢掉**。

    实测（基础容量 10 万、目标误判率 `1e-6`）：3 倍容量时每 13 个新 URL 丢 1 个，
    **5 倍容量时丢掉一半**，8 倍时基本不再发现新页面。而 100 万对分布式爬虫
    根本不是大数目 —— 这个上限恰好卡在框架的核心卖点上。

    现在一层填满就加一层：容量 ×2、误判率 ×0.5，各层误判率成等比数列，
    **总误判率收敛到目标值的 2 倍以内**。同样的曲线重跑，8 倍容量下误判率仍是 0%。

    `DEDUP_MAX_LAYERS` 默认 4（容量 ×15，内存 57MB）。**层数必须有顶** ——
    无限加层就是把内存变成无界资源，那正是 0.9.0 刚从下载路径上清掉的东西。

    升级不会让已有去重状态失效：第 0 层沿用原来的 key。

- **`DEDUP_WARN_FILL_RATE`（默认 0.8）—— 去重容量告警。** 填到八成就走告警通道
  （飞书 / 邮件）。在这之前**完全没有任何信号**：计数一直有记录但没人读，
  统计里只显示「去重 N 条」，爬虫只是「提前结束了」，查不出原因。

### 修复

- **`RedisBloomFilter.add` 存在跨节点竞态。** 原来用 pipeline 的 SETBIT ——
  pipeline 只是打包发送、**并不保证原子**，两个节点会同时把同一个 key 判定为
  「新」，于是同一个 URL 被抓多遍。改用 Lua 脚本后置位与计数一次完成且原子。

    真 Redis + 三进程并发验证：把实现换回非原子的「先查再写」时用例立刻转红。
    fakeredis + 单进程测不出这个 —— 顺带把 `fakeredis[lua]` 加进了开发依赖，
    否则 Redis 布隆这条路径会因为 `unknown command 'evalsha'` 悄悄没被测到。


- `render=True`（Playwright）此前**绕过 `MAX_RESPONSE_SIZE`** —— 渲染结果是直接
  从浏览器取的，没走流式下载那条路，等于给大小上限开了个后门。现在渲染结果同样
  认上限。注意这条路只能在拿到整页 HTML **之后**判定：它挡的是「把巨大的字符串
  交给用户的 `parse()`」，挡不住浏览器进程那边的内存。

## [0.9.0] - 2026-09-07

两块**边界**：跨节点的速率边界，和单个响应的资源边界。

⚠️ **含一处行为变更**：响应体默认上限 32MB（`MAX_RESPONSE_SIZE = 0` 可放开）。

### 新增

- **`MAX_RESPONSE_SIZE` —— 响应体大小上限**（默认 32MB，`0` = 不限）。下载改为
  **流式**：先拿响应头，再决定要不要读 body、读多少。

    此前框架会把**任何**响应整个读进内存。实测 200MB 的响应让进程 RSS 涨
    **618MB**（`bytes` 一份、`.text` 解码又一份），默认 4 线程同时撞上 ~2.5GB。
    容器里这就是 OOM —— 而 **OOM Killer 发 `SIGKILL`**，绕过 0.8.1 刚修好的优雅
    停止，于是又回到「节点本地缓冲里的任务永久丢失」。这两件事是连着的。

    实测（200MB 靶子，上限 32MB）：报了 `Content-Length` 的场景 RSS 只涨 **5MB**
    且**服务端一个字节的 body 都没发出**；分块传输不报长度的场景涨 50MB、
    服务端发出 33MB 后被断开。

    超限抛 `ResponseTooLargeError`：**不重试**（再抓一次还是一样大），
    **不计入熔断**（响应大是「这个 URL 太大」，不是「这个站挂了」——
    否则一个站上几个大 PDF 就能把整域熔断）。

    **这是行为变更**：确实在下载大文件的爬虫会被拦下，报错里点名了设置项，
    放开就是 `MAX_RESPONSE_SIZE = 0`。

- **`ALLOWED_CONTENT_TYPES` —— Content-Type 前缀白名单**（默认空 = 不过滤）。
  命中不了的响应**一个字节的 body 都不读**就断开，省的是带宽。被挡掉算丢弃
  而不是失败。没有 `Content-Type` 头的响应一律放行 —— 不少站点根本不发这个头。

- **`GLOBAL_THROTTLE` —— 跨节点全局限速**。打开后 `DOWNLOAD_DELAY` 由 Redis 记账，
  所有节点共用一份「该域下次可请求的时刻」，N 个节点**合起来**才是配置的那个速率；
  429 的整域冷却同样全局生效，一个节点撞上限速所有节点一起避开。

    此前限速只在进程内生效，分布式起 N 个节点目标站就承受 N 倍 —— 这个限制一直
    只能写在文档里。三节点实测（`DOWNLOAD_DELAY=0.3`，判据取自 HTTP 靶子记录的
    到达时刻）：关闭时峰值 **10 请求/秒**（正好是配置值的 3 倍），打开后 **4 请求/秒**。

    取号用 Lua 保证原子，一次往返就算出准确的等待时长，不做「没令牌就重试」的轮询；
    时钟取自 Redis 服务端，避免节点间时钟偏移变成限速误差。
    Redis 不可用时**退回进程内限速**（不是退回不限速）并告警一次。

    默认关闭；并发上限 `CONCURRENT_REQUESTS_PER_DOMAIN` 仍是进程内的。

## [0.8.1] - 2026-09-07

分布式节点的数据丢失修复。**容器化部署的用户建议尽快升级。**

### 修复

- **节点被 `SIGTERM` 停止时会永久丢失已领取的任务** —— 框架只安装了 `SIGINT`
  处理器。`Collector` 一次从 Redis 领走最多 `COLLECTOR_TASK_COUNT`（默认 100）个任务，
  而队列用 `zpopmin`（取走即删）；`SIGTERM` 没有处理器 → 进程直接终止 →
  `_on_shutdown` 里「把本地缓冲推回 Redis」的逻辑没机会执行。

    实测 24 个任务的场景：`SIGTERM` 丢 20 个，`SIGINT` 全数恢复。而 `SIGTERM` 正是
    `docker stop` / Kubernetes 驱逐 / `systemctl stop` 所发的信号 —— 容器化部署下
    每次停节点都在丢任务。现在两个信号都走优雅停止。

- `robots.txt` 规则判定异常时不再完全静默 —— 此前 `can_fetch` 抛异常会直接放行
  且无任何日志，若对每个 URL 都抛异常会「全部放行」而无人察觉。现在告警一次。

### 说明

新增真 Redis + **真多进程**的集成测试。此前 839 行分布式代码全部只用 `fakeredis` +
单进程测过 —— 既没有真正的并发竞争，也不跨进程。上面那个 `SIGTERM` 缺陷就是它抓出来的。

### 新增

- **`examples/`** —— 可以直接跑的完整示例。首个是
  [`books_toscrape.py`](https://github.com/apersonw/mineworker/tree/main/examples)：
  两级抓取（列表页翻页 → 详情页）、`cb_kwargs` 传状态、`Item` + `__unique_key__` 去重，
  以及真实站点上该配的礼貌性设置。抓的是 Zyte 专为爬虫练习搭建的站点，可以放心跑。

    配套两层防腐测试：结构检查（不联网，进 CI）+ 真跑一遍（`network` 标记，
    不进 CI）—— 站点改版让选择器失效时，只有后者能发现。

## [0.8.0] - 2026-09-06

长跑生存：目标站挂了别死磕，定时任务跑够能停。

### 新增

- **按域熔断** —— 同域**连续**失败到 `CIRCUIT_FAILURE_THRESHOLD`（默认 10）次时，
  该域进入 `CIRCUIT_COOLDOWN`（默认 60s）冷却，所有工作线程一起避让
  （复用 per-domain 限速的整域降速机制）。

    **只数「站点不健康」的信号**：网络错误、429、5xx。**404 等 4xx 不计** ——
    按 ID 顺序探测时连续几十个 404 很正常，拿它跳闸会把正常爬取搞瘫；
    解析异常 / 校验失败同样不计，那是爬虫自己的问题。

    计数发生在**重试耗尽之后**，这样代理池有机会先轮换出口 ——
    「代理坏了」会被重试吸收，只有站点真的挂了才会连续走到这一步。

- **运行时长上限** —— `SPIDER_MAX_RUNTIME`（默认 `0` = 不限）。到点走优雅停止：
  flush 缓冲区、dump 未完成请求，然后**正常返回不抛异常** ——
  定时任务「跑够就停」不该被当成错误。

### 说明

顺带做了一次长跑内存画像，结论是**没有泄漏**：16 线程跑 600 秒、约 11 万请求后
RSS 在 ~127MB 收敛（最后 1/4 斜率 +0.22 MB/分钟），每线程边际成本约 0.05MB。
工具在 [`benchmarks/soak.py`](https://github.com/apersonw/mineworker/blob/main/benchmarks/soak.py)。

## [0.7.0] - 2026-09-05

礼貌性与失败处理。**含一处破坏性变更**，升级前请读下面第一节。

### ⚠️ 破坏性变更：非 2xx 响应不再进 `parse()`

0.6.0 及以前框架**完全不检查状态码**：`validate()` 默认返回 `True`，于是
429 / 503 / 404 的响应体直接进 `parse()` 被当成数据 —— 被限速时不但不退避，
还会把限速提示页入库，然后继续重试。

0.7.0 起：

| 状态码 | 处理 |
|---|---|
| 2xx / **3xx** | 正常进 `parse()`（3xx 能到回调说明你显式关了 `allow_redirects`） |
| 429 / 500 / 502 / 503 / 504 | 重试（可配 `RETRY_STATUS_CODES`） |
| 其余非 2xx | 判失败，走 `failed_request()` 钩子 |

**迁移**：
- 想让 `parse()` 继续收到 404 之类 → `ACCEPT_STATUS_CODES = [404]`
- 想完全回到旧行为 → `CHECK_STATUS_CODE = False`

### 新增

- **robots.txt** —— `ROBOTS_OBEY`（库默认 `False`，但 `mineworker create -p` 生成的
  项目配置里写 `True`，新项目开箱合规）、`ROBOTS_USER_AGENT`、`ROBOTS_CACHE_TTL`。

    按域缓存，多线程首访也只抓一次。被禁止的 URL 不产生请求、计入结束行的
    「robots 拦截」、**不算失败**（有意跳过不该污染失败率）。
    robots.txt 的 `Crawl-delay` 会自动接管该域限速，取 `max(DOWNLOAD_DELAY, Crawl-delay)`。

    抓不到 robots.txt 时（404 / 5xx / 超时）**放行**并打 warning ——
    一次瞬时 500 不该让整个爬虫停摆。

    小数 `Crawl-delay` 自行解析：标准库 `RobotFileParser` 只接受整数
    （用 `isdigit()` 判断），会静默丢弃 `Crawl-delay: 0.5` —— 那等于爬得比站点
    要求的还快。

- **per-domain 限速** —— 按域名分账的并发上限与请求间隔：
  `CONCURRENT_REQUESTS_PER_DOMAIN`（默认 `8`）、`DOWNLOAD_DELAY`（默认 `0`，不限）、
  `RANDOMIZE_DOWNLOAD_DELAY`（默认 `True`）。

    默认上限 8 大于默认线程数 4，所以**对默认配置无感** —— 它是调大
    `SPIDER_THREAD_COUNT` 时的安全网，而不是给所有人降速。

    收到 429 / 503 的 `Retry-After` 时，冷却作用在**整个域名**上，所有工作线程一起
    避开（只让撞上的那个线程等，其余线程会继续满速打同一个域，退避形同虚设）。

    ⚠️ **这是进程内限速**：分布式起 N 个节点，目标站承受 N 倍。全局限速需要 Redis
    令牌桶，尚未实现 —— 多节点部署请自行按节点数折算。

- **`Retry-After` 退避** —— 429 / 503 重试时读该头（秒数与 HTTP-date 都支持），
  按服务端要求等待。超过 `RETRY_AFTER_MAX`（默认 60s）则不再等待、直接判失败 ——
  等十分钟不值得占着一个工作线程
- **指数退避** —— `RETRY_BACKOFF > 0` 时按 `base × 2^(重试次数-1)` 等待并加抖动
  （抖动避免多个 worker 同步重试），封顶 `RETRY_AFTER_MAX`
- 新异常 `HttpStatusError(RequestError)`，带 `.status_code`

等待时长**只有一个计算入口**，优先级固定：`Retry-After` > 指数退避 > `SPIDER_RETRY_INTERVAL`。

### 说明

这一版的动因是 0.6.0：它把默认吞吐提高了约 3.2×，而当时框架**没有任何限速与失败处理**。
更快地打目标站、同时把错误页当数据入库，是需要马上补上的责任缺口。
per-domain 限速与 robots.txt 在后续版本。

## [0.6.0] - 2026-09-05

存储扩展 + 一次由实测驱动的性能修复。**升级即得约 3.2× 吞吐，无需改任何配置。**

### 性能

- **默认配置下吞吐提升约 3.2×**（50ms 延迟、32 线程：97 → 308 QPS）——
  缓存 `SSLContext`。`httpx.Client()` 每次构造都会新建 SSL context（加载 CA 包，
  实测 **32.9ms/个**），而下载器默认每个请求新建一个 Client，于是这 33ms 成了
  每请求的固定开销 —— 框架最大的单项成本。缓存后降到 **0.4ms**。

    **抓取语义零变化**：`SSLContext` 是无状态配置对象，cookie 仍然每请求隔离。
    这与「共享 `Client`」不同 —— 后者会连 cookie jar 一起共享。

- 顺带解决了「线程越多越慢」：那 33ms 的 CA 解析占着 GIL，本身就是争用源。
  修复前吞吐在 ~100 QPS 封顶，现在随线程数单调增长（4→128 线程：67 → 555 QPS）

### 新增

- **PostgreSQL 管道** —— `PostgresPipeline`（psycopg 3）。`ON CONFLICT` 三种模式：
  `nothing`（默认，冲突跳过）/ `update`（upsert，需 `POSTGRES_CONFLICT_TARGET`）/
  `error`。需 `pip install "mineworker[postgres]"`
- **Elasticsearch 管道** —— `helpers.bulk` 批量写，`__update_key__` 拼 `_id` 做 upsert
- **Kafka 管道** —— `table_name` 当 topic。它是投递而非存储，不支持 `UpdateItem`
- 抽出 `SqlPipeline` 基类，MySQL / PostgreSQL 共用同一套写入骨架
- **真实数据库集成测试** —— Postgres 与 MySQL 跑同一组用例，CI 用 service containers
  （`MysqlPipeline` 此前从没跑过真库）
- [`benchmarks/`](https://github.com/apersonw/mineworker/tree/main/benchmarks) 吞吐画像套件

### 修复

- **`setting.USE_SESSION` 是死配置** —— 它在 `setting.py` 有定义、文档里也写着
  「复用 httpx 连接」，但框架代码**从没读过它**，只有 `Request(use_session=)` 生效。
  现在两者都生效（请求级优先）。默认仍为 `False`：开启会让 cookie 跨请求共享

### 说明

0.4.0 曾记载「工作线程是 1 线程 1 在途」并建议「调大 `SPIDER_THREAD_COUNT` 到 ~100」。
实测表明**两者都不准确**：时间加权平均在途只有线程数的 15%–58%，而线程数超过某点后
效率显著下降。同时 **async 批量分发已被实测否决**（瓶颈不在线程模型）。
完整数据见 [async 内核评估](https://apersonw.github.io/mineworker/async-kernel/#实测2026-09)。

## [0.5.0] - 2026-09-04

反爬对抗：从 TLS 握手层解决问题，而不是继续换 User-Agent。

### 新增

- **TLS / HTTP2 指纹伪装** —— 新下载器 `CurlDownloader`（基于
  [curl_cffi](https://github.com/lexiforest/curl_cffi) / libcurl-impersonate）。
  设 `DOWNLOADER_IMPERSONATE = "chrome"` 即启用，**爬虫代码零改动**；
  也可按请求覆盖：`Request(url, impersonate="safari17_0")`。
  需 `pip install "mineworker[curl]"`（已并入 `all`）
- **反爬拦截识别** —— 自动识别 Cloudflare / Akamai 挑战页与 JS 跳转空壳，
  抛 `AntiBotError`。它继承 `RequestError`，因此直接复用既有的重试路径，
  重试时代理池会换一个出口 IP。开关 `ANTIBOT_DETECT`（默认开）
- 新文档：[反爬对抗](https://apersonw.github.io/mineworker/anti-bot/)

### 变更

- **启用 `impersonate` 时不再注入随机 User-Agent**。`impersonate` 自带一整套自洽的
  浏览器头，再叠加 UA 池会造成「TLS 握手说 Chrome、UA 头说 Firefox」的自相矛盾 ——
  这比不伪装更容易被识破。显式传入的 `headers` 仍然优先
- 下载器选择顺序：`render` > `impersonate` > `DOWNLOADER_ASYNC` > `use_session` > 默认
- `Request.impersonate` 参与序列化，分布式模式下经 Redis 传递不会丢失

### 说明

为什么换 UA 不够：现代反爬看的是 TLS 握手指纹（JA3/JA4）和 HTTP/2 SETTINGS 帧 ——
在你发出第一个字节之前就已经暴露。实测同一端点，httpx 是
`JA4 t13d1712h1_…` + HTTP/1.1 而 UA 却自称 Firefox 126（三重矛盾），
伪装后为 `JA4 t13d1516h2_…` + h2 + Chrome UA，三者自洽。

## [0.4.0] - 2026-09-04

**首次发行到 PyPI。** 0.3.0 之后积累的全部分布式能力（v2.1–v2.7）随本版本一次性发出。

### 新增

- **Redis 基础设施** —— `db/redisdb.py`（连接缓存、`acquire_once` 一次性锁、统一 key 前缀）、
  `RedisTaskQueue`（zset 优先级队列，原子 `zpopmin`）、`RedisSetFilter` / `RedisBloomFilter`；
  `DEDUP_FILTER=redis|redis-set` 即可整体切换去重后端
- **`Spider`（分布式）** —— Redis 队列 + Redis 布隆去重 + 断点续爬 + `start_requests` 一次性锁 +
  多节点心跳结束检测 + 失败请求落 Redis。抽出 `BaseScheduler` 公共骨架
- **`TaskSpider`** —— 从 Redis / DB 任务源持续拉任务，多节点分摊，`keep_alive` 常驻
- **账号 / Cookie 池** —— `LocalUserPool` / `GuestUserPool` / `RedisUserPool`；
  `user_pool()` + `check_login()` 钩子，掉登录自动换号重试
- **MySQL 管道** —— `MysqlPipeline`（`executemany` 批量写 + `ON DUPLICATE KEY UPDATE` upsert）、
  `MysqlDB`（PooledDB 连接池）；`mineworker create -i --table <表>` 读 `SHOW FULL COLUMNS` 反射生成 Item
- **`AsyncHttpxDownloader`** —— 可选异步下载器（`DOWNLOADER_ASYNC=True`），独立事件循环线程 +
  共享 `AsyncClient`，爬虫代码零改动；新增 `HTTPX_HTTP2` 开关
- **`BatchSpider`** —— 周期性批次采集：MySQL 任务表状态机 + 批次记录表 + 进度追踪 + 任务防丢；
  master（`start_monitor`）/ worker（`start`）分离，抽象 `BatchStore`（`MysqlBatchStore` / `MemoryBatchStore`）
- 文档站新增《分布式》《批次采集》《账号 / Cookie 池》《async 内核评估》四篇

### 修复

- `cb_kwargs` 从未传给 callback（`parser_control` 现在按 `callback(request, response, **request.cb_kwargs)` 调用）
- `BaseScheduler.run()` 中 `_seed()` 移到 `_start_threads()` 之前，避免 worker 抢跑断点续爬遗留的队列
- **刚启动的机器 / 容器上第一条告警被静默吞掉**：`AlertManager` 用 `0.0` 当「从没发过」的哨兵，
  而 `time.monotonic()` 的原点是开机，`now - 0.0 < WARNING_INTERVAL` 在低 uptime 时恒真。
  现在用「key 缺席」表示从没发过
- **同源问题让代理池在新容器里起不来**：`ApiProxyPool` 的 `_last_fetch` 初值 `0.0` 会让第一次
  拉取代理被间隔限流跳过，池子一直是空的。现在初值为 `None`
- 只装核心包（不带 `[cli]`）时执行 `mineworker` 会抛 `ModuleNotFoundError` 堆栈，
  现在提示 `pip install "mineworker[cli]"`

### 变更

- 包元数据：`Development Status` 提到 `4 - Beta`，补 `Typing :: Typed` 与完整的 `[project.urls]`
- 新增 PyPI 发布与文档部署流水线（Trusted Publishing，无 token）

### 说明

- 关于「是否把内核改成全 async」的评估结论见
  [async 内核评估](https://apersonw.github.io/mineworker/async-kernel/)：**不做全量重写**，
  只落地隔离的异步下载器。工作线程仍是「1 线程 1 在途」，`AsyncHttpxDownloader` 的收益是
  连接复用 / HTTP2 / 更低 FD

## 0.3.0 - 2026-09-03

轻量单机版（`AirSpider`）完整可用。**未发行到 PyPI**（当时仅本地开发）。

### 新增

- **运行时** —— 内存优先级队列、`Collector` / `ParserControl` / `RequestBuffer` / `ItemBuffer`、
  优雅退出、`AirSpider`
- **网络层** —— `Request` / `Response`（w3lib 编码检测 + parsel 选择器）、`Downloader` ABC +
  `HttpxDownloader`、重试与超时
- **数据与去重** —— `Item` / `UpdateItem`、内存布隆过滤器、
  `Console` / `CSV` / `Mongo` 管道
- **浏览器渲染** —— `PlaywrightDownloader` + 渲染池，`Request(render=True)`
- **中间件与代理** —— `DownloaderMiddleware` 链、`ProxyPool` ABC + `ApiProxyPool`
- **可观测性** —— `MetricsReporter` + Prometheus exporter、`AlertManager`（飞书 / 邮件 / 日志）
- **命令行** —— `mineworker create` 脚手架、`shell` 交互调试、`retry` 失败重放
- mkdocs-material 文档站

[Unreleased]: https://github.com/apersonw/mineworker/compare/v0.8.1...HEAD
[0.8.1]: https://github.com/apersonw/mineworker/releases/tag/v0.8.1
[0.8.0]: https://github.com/apersonw/mineworker/releases/tag/v0.8.0
[0.7.0]: https://github.com/apersonw/mineworker/releases/tag/v0.7.0
[0.6.0]: https://github.com/apersonw/mineworker/releases/tag/v0.6.0
[0.5.0]: https://github.com/apersonw/mineworker/releases/tag/v0.5.0
[0.4.0]: https://github.com/apersonw/mineworker/releases/tag/v0.4.0
