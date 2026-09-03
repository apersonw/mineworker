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

## 重试

下载失败、`validate` 抛 `ValidationError`、`parse` 抛非 `NotRetryError` 异常 →
`retry_times += 1` 重新入队（跳过去重）；超过 `SPIDER_MAX_RETRY_TIMES` →
调 `failed_request`，并把请求 dump 到 `failed_requests.jsonl`（`mineworker retry --requests` 可回放）。

## 优雅退出

`Ctrl-C` 一次：停止取新任务、排空在途请求、flush 缓冲、dump 未完成请求。再按一次：强制退出。
