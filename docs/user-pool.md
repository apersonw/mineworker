# 账号 / Cookie 池

需要登录才能抓、或者想用一批账号轮流抓、被封了自动换号 —— 用账号池。

爬虫里覆写两个方法即可，调度器会自动把池子挂到下载链上：

```python
import mineworker as mw


class MallSpider(mw.Spider):
    def user_pool(self):
        return mw.LocalUserPool(
            users=[
                {"username": "u1", "password": "p1"},
                {"username": "u2", "password": "p2"},
            ],
            login=self.login,          # 账号没 cookie 时调用
        )

    def login(self, user):
        resp = mw.Request(
            "https://mall.com/api/login",
            method="POST",
            data={"user": user.username, "pwd": user.password},
        ).download()
        return dict(resp.cookies)      # 返回 cookies dict

    def check_login(self, response):
        return "请先登录" not in response.text   # False -> 拉黑当前账号，换号重试

    def start_requests(self):
        yield mw.Request("https://mall.com/orders", callback=self.parse)

    def parse(self, request, response):
        ...
```

每个请求下载前自动挂上某个账号的 cookie；下载后 `check_login` 判断是否掉登录，
掉了就 `report_bad`（默认拉黑 30 分钟、清掉缓存的 cookie）并换一个账号重试。

## 几种池子

| 类 | 场景 |
|---|---|
| `LocalUserPool` | 单机。给一批账号，轮流用，被封的拉黑一段时间 |
| `GuestUserPool` | 无账号，只要匿名 cookie。`login()` 拿一份游客 cookie，维护固定数量的游客会话 |
| `RedisUserPool` | 多进程 / 多机共享一批账号 —— **不会两个节点同时用同一个号**，cookie 缓存在 Redis |

```python
# 游客
def user_pool(self):
    return mw.GuestUserPool(login=self.get_guest_cookie, size=5)

# 分布式共享账号
def user_pool(self):
    return mw.RedisUserPool(
        "MallSpider",
        accounts=[{"username": "u1", "password": "p1"}, ...],
        login=self.login,
        cookie_ttl=3600,
    )
```

## 手动用

```python
pool = mw.LocalUserPool([{"username": "u1"}], login=my_login)
user = pool.get()
try:
    resp = mw.Request(url, cookies=user.cookies).download()
    pool.report_ok(user)
except SomethingBad:
    pool.report_bad(user, block_seconds=600)
```

单个请求也能直接指定 cookie：`mw.Request(url, cookies={"sid": "..."})`。
