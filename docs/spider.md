# 编写爬虫

## AirSpider

继承 `mw.AirSpider`，实现 `start_requests`（产出种子请求）和 `parse`（解析）：

```python
class MySpider(mw.AirSpider):
    __custom_setting__ = dict(SPIDER_THREAD_COUNT=8)  # 合并进全局配置

    def start_requests(self):
        yield mw.Request("https://example.com", callback=self.parse)

    def parse(self, request, response):
        yield mw.Request(response.urljoin("/next"), callback=self.parse_detail)
        yield {"url": request.url}          # dict 或 Item 都行

    def parse_detail(self, request, response):
        ...

MySpider().start()          # 阻塞至爬完并优雅退出
```

`parse` / callback 可以 `yield`：

| yield 的东西 | 去向 |
|---|---|
| `mw.Request` | 回到调度队列（去重后） |
| `mw.Item` / `dict` | 进 ItemBuffer → 批量落库 |
| 可调用对象 | 在当前工作线程执行 |

## Request

```python
mw.Request(
    url,
    method="GET",
    callback=self.parse,          # 方法、函数或方法名字符串
    priority=300,                 # 越小越先出队
    retry_times=0,
    filter_repeat=True,           # 是否参与去重
    render=False,                 # 浏览器渲染，见「浏览器渲染」
    cb_kwargs={"page": 2},        # 传给 callback 的额外参数
    headers=..., params=..., data=..., json=..., cookies=..., timeout=...,  # 透传给 httpx
    batch_id=7,                   # 未知参数 -> request.batch_id
)
```

`response.xpath / .css / .re / .re_first / .text / .json() / .urljoin() / .status_code`。
`.xpath(...).extract_first()` 也可用（parsel 的 `.get()` 别名）。

## 钩子

```python
class MySpider(mw.AirSpider):
    def download_midware(self, request):
        request.headers = {"Referer": "https://example.com"}
        return request                       # 返回 None 用原请求

    def validate(self, request, response):
        if response.status_code != 200:
            raise mw.ValidationError("非 200")   # -> 重试
        if "验证码" in response.text:
            raise mw.NotRetryError("被封")       # -> 直接丢弃
        return True                              # False 也表示丢弃

    def failed_request(self, request, response):
        # 重试耗尽后调用；可再 yield Request / Item
        yield {"failed": request.url}

    def start_callback(self): ...   # 启动时一次
    def end_callback(self): ...     # 正常结束时一次
```

## 状态码策略

!!! warning "0.7.0 破坏性变更"
    0.6.0 及以前**不检查状态码** —— `validate()` 默认返回 `True`，于是 429 / 503 / 404 的
    响应体会直接进 `parse()` 被当成数据。被限速时不但不退避，还会把限速提示页入库。
    0.7.0 起默认按下表处理。**要回到旧行为：`CHECK_STATUS_CODE = False`。**

| 状态码 | 处理 |
|---|---|
| 2xx | 正常进 `parse()` |
| **3xx** | 正常进 `parse()`。能走到回调说明你显式关了 `allow_redirects`，那就是你要的 |
| `RETRY_STATUS_CODES`（默认 429 / 500 / 502 / 503 / 504） | 重试；429 与 503 会读 `Retry-After` |
| `ACCEPT_STATUS_CODES`（默认空） | 正常进 `parse()` |
| 其余非 2xx（404 / 403 …） | 判失败，走 `failed_request()` 钩子 |

想让 `parse()` 自己处理 404：

```python
ACCEPT_STATUS_CODES = [404]
```

### 退避

重试前等多久，按固定优先级：

1. **`Retry-After` 头**（仅 429 / 503）—— 服务端明说了等多久就听它的，秒数与 HTTP-date 都支持。
   超过 `RETRY_AFTER_MAX`（默认 60s）则不再等待、直接判失败 —— 等十分钟不值得占着一个工作线程
2. **`RETRY_BACKOFF > 0`** → 指数退避 `base × 2^(重试次数-1)` + 抖动，封顶 `RETRY_AFTER_MAX`。
   抖动是为了避免多个 worker 同步重试
3. 否则 `SPIDER_RETRY_INTERVAL`（默认 `0.0`）

!!! note "等待发生在工作线程内"
    这意味着退避期间该线程不处理别的请求。被限速时这反而是好事 —— 天然降低了对目标站的压力。

## 重试

下载失败、状态码不被接受、`validate` 抛 `ValidationError`、`parse` 抛非 `NotRetryError` 异常 →
`retry_times += 1` 重新入队（跳过去重）；超过 `SPIDER_MAX_RETRY_TIMES` →
调 `failed_request`，并把请求 dump 到 `failed_requests.jsonl`（`mineworker retry --requests` 可回放）。

## 优雅退出

`Ctrl-C` 一次：停止取新任务、排空在途请求、flush 缓冲、dump 未完成请求。再按一次：强制退出。
