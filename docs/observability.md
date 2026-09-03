# 监控与调试

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
WARNING_FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
WARNING_EMAIL = dict(host="smtp.qq.com", port=465, ssl=True,
                     user="bot@qq.com", password="***", to=["me@corp.com"])

WARNING_FAILED_RATE = 0.5      # 失败率超 50% 告警
WARNING_MIN_REQUESTS = 50      # 少于 50 个请求不算失败率
WARNING_FAILED_COUNT = 1000    # 失败数超 1000 告警
WARNING_STALL_SECONDS = 600    # 10 分钟没有新的成功请求 = 卡死
WARNING_INTERVAL = 300         # 同类告警最小间隔，防刷屏
```

调度器在结束检测循环里顺带跑告警检查。默认还有一个 `LogNotifier`（写 WARNING 日志）。

## 调试

```python
NewsSpider(debug=True).start()
```

`debug=True` → 日志转 `DEBUG`、强制单线程，方便逐条跟踪。等价于
`MINEWORKER_DEBUG=true MINEWORKER_LOG_LEVEL=DEBUG python main.py`。

## 崩溃恢复

- `failed_requests.jsonl` —— 中断退出时未完成的请求；`mineworker retry --requests` 重新下载
- `failed_items.jsonl` —— 写库失败的数据；`mineworker retry --items` 重放到管道
