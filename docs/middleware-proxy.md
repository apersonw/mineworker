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
```

`HttpxDownloader` 会在请求没有显式代理时从池里取一个；下载报错时自动 `report_bad` 丢弃该代理。
池空时（且距上次拉取超过 `PROXY_MIN_INTERVAL`）重新拉取。

自定义代理池：继承 `mineworker.network.proxy_pool.base.ProxyPool`，实现 `get_proxy()`，
然后 `PROXY_POOL = "myproj.MyProxyPool"`。

单个请求也可指定：`mw.Request(url, proxy="http://user:pass@host:port")`。
