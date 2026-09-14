# 监控与调试

## 运行摘要（机器可读）

每次爬虫结束，除了给人看的那行「爬虫结束 | 请求成功 63 …」，还会往 **stdout**
再打一行**给机器看**的：

```
MINEWORKER_RUN_SUMMARY {"schema":1,"request_ok":63,"items":60,"run_id":"...","spider":"BookSpider",...}
```

为什么单列一行：编排层（如 MineWorkerHub）读容器日志判成败，此前只能看 exit code——
一个空转的实例（种子锁还在、队列空、`request_ok=0`）照样 exit 0，被记成成功。
生产上一个定时任务两天里 1158/1160 个实例就是这么静默空转的（见
[运行作用域](distributed.md#运行作用域定时重跑)）。这行摘要让「跑了」和「抓了」
从日志里一眼可分。

- **走 stdout，不走日志**：不受 `LOG_LEVEL` 影响（有人把 worker 压到 `WARNING`），
  也不带 loguru 的时间戳 / 颜色——解析方按前缀 `MINEWORKER_RUN_SUMMARY ` 找到该行、
  取其后的一段当 JSON 即可。
- 字段是**对外契约**：前缀、`schema`、各计数键名都保持稳定；字段增删时 `schema` +1。
- 关掉：`RUN_SUMMARY_ENABLE = False`。**在进程内嵌入爬虫、自己解析 stdout 的工具**
  （基准脚本、测试 harness）应关掉——那一行会混进你自己的输出里。

判「空转」：`request_ok == 0 and items == 0`（而进程 exit 0）。
`run_id` / `namespace` 只在设了[运行作用域](distributed.md#运行作用域定时重跑)时非空，
用来把这行关联到具体实例 / 运行。

## 指标

```python
METRICS_ENABLE = True
METRICS_LOG_INTERVAL = 10          # 每 10 秒打一行进度
METRICS_PROMETHEUS_PORT = 9100     # >0 且装了 mineworker[metrics] 时起 exporter
```

进度行：

```
metrics - 进度 | 成功 1240 失败 12 重试 30 | 队列 88 在途 4 | 入库 1180 | 21.3 请求/s
```

Prometheus（`http://localhost:9100/metrics`）：

```
mineworker_request_ok 1240.0
mineworker_request_failed 12.0
mineworker_item 1180.0
mineworker_queue_depth 88.0
mineworker_in_flight 4.0
```

## 告警

```python
WARNING_ENABLE = True
# 群机器人（配几个就发几个）
WARNING_FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
WARNING_DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=xxx"
WARNING_DINGTALK_SECRET = "SECxxx"     # 「加签」模式的密钥；用「自定义关键词」则留空
WARNING_WECHAT_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"

WARNING_EMAIL = dict(host="smtp.qq.com", port=465, ssl=True,
                     user="bot@qq.com", password="***", to=["me@corp.com"])

WARNING_FAILED_RATE = 0.5      # 失败率超 50% 告警
WARNING_MIN_REQUESTS = 50      # 少于 50 个请求不算失败率
WARNING_FAILED_COUNT = 1000    # 失败数超 1000 告警
WARNING_STALL_SECONDS = 600    # 10 分钟没有新的成功请求 = 卡死
WARNING_INTERVAL = 300         # 同类告警最小间隔，防刷屏
```

调度器在结束检测循环里顺带跑告警检查。默认还有一个 `LogNotifier`（写 WARNING 日志）。

### 钉钉的两种安全模式

钉钉群机器人必须配安全设置，两选一：

- **加签** —— 填 `WARNING_DINGTALK_SECRET`，框架自动带上 `timestamp` 和签名
- **自定义关键词** —— 不填 secret；消息标题固定带 `【MineWorker】`，
  把关键词设成 `MineWorker` 即可

企业微信没有签名机制，webhook 里的 key 就是凭据。

### 发送失败会被记下来

三家群机器人在 **webhook 失效、关键词不匹配、需要加签**这些情况下**都返回 HTTP 200**，
真正的结果在响应体的错误码里。所以框架会检查状态码**和**响应体，被拒绝时记 ERROR 日志：

```
钉钉告警被拒绝：errcode=310000 keywords not in content
```

!!! warning "为什么要专门检查这个"
    告警系统静默失效是最坏的一种失效 —— 出事那天你才发现它自己早就哑了。
    配好之后建议先手动触发一次（比如把 `WARNING_STALL_SECONDS` 调到 1 秒跑一遍），
    确认群里真的收得到。

## 调试

```python
NewsSpider(debug=True).start()
```

`debug=True` → 日志转 `DEBUG`、强制单线程，方便逐条跟踪。等价于
`MINEWORKER_DEBUG=true MINEWORKER_LOG_LEVEL=DEBUG python main.py`。

## 崩溃恢复

- `failed_requests.jsonl` —— 中断退出时未完成的请求、以及重试耗尽的请求。
  `mineworker retry --requests` 是**探活**（重新下载看状态码，不跑回调、不入库，记录一条不删）；
  要把数据抓回来，开 `RETRY_FAILED_ON_START` 重跑一次爬虫 —— 那会把这些请求灌回队列，
  走完整的下载 → 回调 → 落库
- `failed_items.jsonl` —— 写库失败的数据；`mineworker retry --items` 重放到管道
