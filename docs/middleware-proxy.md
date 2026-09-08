# 中间件与代理

## 下载中间件

在下载前后统一处理所有请求（加签名、换 Cookie、统计等）。

```python
from mineworker.network.middleware import DownloaderMiddleware


class SignMiddleware(DownloaderMiddleware):
    def process_request(self, request):
        request.params = {**request.requests_kwargs.get("params", {}), "sign": sign(request.url)}
        return request                    # 返回 Response 则短路下载

    def process_response(self, request, response):
        if response.status_code == 202:
            return request                # 返回 Request 则丢回队列重新调度
        return response
```

`setting.py`：

```python
DOWNLOADER_MIDDLEWARES = ["myproj.middlewares.SignMiddleware"]
```

`process_request` 按列表顺序执行，`process_response` 逆序。爬虫自己的 `download_midware`
方法仍然有效，在全局中间件之后执行。

## 代理池

```python
PROXY_ENABLE = True
PROXY_EXTRACT_API = "http://proxy-provider.com/get?count=10"   # 返回每行一个代理，或 JSON 数组
PROXY_MAX_USE_TIMES = 100      # 单个代理用满这么多次后轮换
PROXY_WAIT_TIMEOUT = 30.0      # 池空时最多等多久，超时抛 ProxyUnavailableError
PROXY_ALLOW_DIRECT = False     # True 才允许「没代理就直连」
```

`HttpxDownloader` 会在请求没有显式代理时从池里取一个；下载报错时自动 `report_bad` 丢弃该代理。

!!! warning "拿不到代理时**不会**直连"
    代理用满 `PROXY_MAX_USE_TIMES`、被 `report_bad` 丢弃、或取号接口断供，
    都会让池空掉。这时框架**不会**退回直连 —— 那会把源 IP 暴露给目标站，
    而开代理池的全部意义就是别这么干。

    池空时先等 `PROXY_WAIT_TIMEOUT`（默认 30 秒）并按 `PROXY_MIN_INTERVAL`
    的节奏重新取号；仍拿不到才抛 `ProxyUnavailableError`。
    该请求走正常重试，重试用尽后落到 `failed_requests.jsonl`。
    `mineworker retry --requests` 只是**探活**（重新下载看状态码，不跑回调、不入库）；要把数据真正抓回来，开 `RETRY_FAILED_ON_START` 重跑一次爬虫。

    这个错误**不计入熔断** —— 代理供应是自己这边的问题，
    算进去的话代理商断供五分钟就能把所有域全熔断一遍。

    确实想要「有代理就用、没有就直连」，把 `PROXY_ALLOW_DIRECT = True` 打开 ——
    显式配置就不算静默。

    早先的版本在这里返回 `None`，请求就那样直连出去了：实测一次断供后
    21 个请求里有 16 个从本机 IP 打到了靶子，没有任何提示。
池空时（且距上次拉取超过 `PROXY_MIN_INTERVAL`）重新拉取。

自定义代理池：继承 `mineworker.network.proxy_pool.base.ProxyPool`，实现 `get_proxy()`，
然后 `PROXY_POOL = "myproj.MyProxyPool"`。

单个请求也可指定：`mw.Request(url, proxy="http://user:pass@host:port")`。
