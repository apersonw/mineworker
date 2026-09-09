"""全特性同开的一次真实运行 —— 判据全取框架外部。

单元测试测的是**隔离**的特性；这个项目找到的性能与正确性问题几乎都藏在**组合**里
（代理池 × 下载器、session × 线程数、robots × 去重 × 重试）。所以这里不拆开测，
而是把能开的都打开、跑一遍真站点，然后问靶场「你到底收到了哪些请求」。

判据不看框架的 stats（那是框架自己说自己），只看：
- 靶场每条路径被请求了几次
- 数据最终有没有进真库（需要 `MINEWORKER_TEST_POSTGRES_URL`，没配就 skip）
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import mineworker as mw
from integration_site import Site
from mineworker import setting
from mineworker.network.downloader import close_default_downloaders

N_ITEMS = 8


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    yield
    close_default_downloaders()
    setting.reload()


class _Spider(mw.AirSpider):
    def __init__(self, base: str, **kw: Any) -> None:
        self.base = base
        self.seen: list[str] = []
        super().__init__(**kw)

    def start_requests(self) -> Any:
        yield mw.Request(self.base + "/", callback=self.index, use_session=True)

    def index(self, request: Any, response: Any) -> Any:
        for href in response.css("a::attr(href)").getall():
            yield mw.Request(response.urljoin(href), callback=self.item, use_session=True)

    def item(self, request: Any, response: Any) -> Any:
        title = response.css("h1.t::text").get()
        if title:
            self.seen.append(title)


def _configure(**over: Any) -> None:
    setting.reload()
    setting.SPIDER_THREAD_COUNT = 6
    setting.RANDOM_USER_AGENT = True
    setting.USE_SESSION = True
    setting.ROBOTS_OBEY = True
    setting.CONCURRENT_REQUESTS_PER_DOMAIN = 4
    setting.DOWNLOAD_DELAY = 0.0
    setting.SPIDER_MAX_RETRY_TIMES = 2
    setting.RETRY_BACKOFF = 0.01
    setting.CIRCUIT_FAILURE_THRESHOLD = 0  # 关熔断：否则 /boom/500 会被当成整站挂了
    setting.ITEM_PIPELINES = []
    setting.DONE_CHECK_INTERVAL = 0.05
    setting.DONE_CHECK_TIMES = 3
    setting.BUFFER_FLUSH_INTERVAL = 0.05
    setting.LOG_LEVEL = "ERROR"
    for key, value in over.items():
        setattr(setting, key, value)
    from mineworker.utils import log

    log.configure()


def test_everything_at_once_and_the_target_agrees() -> None:
    """一次运行同时验六条规则，每条都由靶场的请求计数说了算。"""
    _configure()
    with Site(n_items=N_ITEMS) as site:
        spider = _Spider(site.url)
        spider.start()
        hits = dict(site.hits)

    assert hits.get("/robots.txt") == 1, "robots.txt 该被取一次"

    # robots：被禁的路径**一次都不能碰**
    assert "/private/secret" not in hits, f"robots 没拦住，/private/ 被抓了：{hits}"

    # 去重：首页里 /item/0 和 /item/1 各出现两次，但每个页面只该抓一次
    for i in range(N_ITEMS):
        assert hits.get(f"/item/{i}") == 1, f"/item/{i} 抓了 {hits.get(f'/item/{i}')} 次"

    # 4xx 不重试
    assert hits.get("/boom/404") == 1, f"404 被重试了：{hits.get('/boom/404')} 次"

    # 5xx 重试到耗尽：1 次首发 + SPIDER_MAX_RETRY_TIMES 次重试
    want = 1 + setting.SPIDER_MAX_RETRY_TIMES
    assert hits.get("/boom/500") == want, f"500 抓了 {hits.get('/boom/500')} 次，应为 {want}"

    # 429 重试后成功：靶子前两次返回 429，第三次放行
    assert hits.get("/boom/429") == 3, f"429 抓了 {hits.get('/boom/429')} 次，应为 3"

    # gzip 解压：拿到了正文才会记进 seen
    assert "gz" in spider.seen, "gzip 响应没解出来"
    assert "ok429" in spider.seen, "429 重试成功后没拿到正文"


def test_proxy_pool_carries_the_whole_crawl() -> None:
    """开代理池且**不允许直连**时，整轮抓取必须全程走代理。

    `PROXY_ALLOW_DIRECT=False` 下，代理若没被真正使用，请求会失败而不是偷偷直连 ——
    所以「结果正确」本身就证明了流量走的是代理。
    """
    tinyproxy = pytest.importorskip("shutil").which("tinyproxy") or __import__("os").path.exists(
        "/usr/local/opt/tinyproxy/bin/tinyproxy"
    )
    if not tinyproxy:
        pytest.skip("需要 tinyproxy：brew install tinyproxy")

    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))
    from proxylab import StaticPool, proxy

    with Site(n_items=N_ITEMS) as site, proxy() as purl:
        _configure(
            PROXY_ENABLE=True,
            PROXY_POOL="proxylab.StaticPool",
            PROXY_ALLOW_DIRECT=False,
            ROBOTS_OBEY=False,
        )
        StaticPool.urls = [purl]
        spider = _Spider(site.url)
        spider.start()
        hits = dict(site.hits)

    for i in range(N_ITEMS):
        assert hits.get(f"/item/{i}") == 1, f"走代理时 /item/{i} 没抓到或抓重了"
    assert len(spider.seen) >= N_ITEMS


def test_items_land_in_a_real_database(postgres_db: Any) -> None:
    """整链：抓取 → 解析 → ItemBuffer → 真 PostgreSQL。

    此前 `test_sql_integration.py` 只测到 pipeline 那一层，
    「爬虫跑完数据真进了库」这条整链没有覆盖。
    """

    class Row(mw.Item):
        __table_name__ = "fullstack_items"
        __unique_key__ = ["url"]
        url: str = ""
        title: str = ""

    class DbSpider(_Spider):
        def item(self, request: Any, response: Any) -> Any:
            title = response.css("h1.t::text").get()
            if title:
                yield Row(url=request.url, title=title)

    # pipeline 读的是 setting.POSTGRES_*，而 _configure() 里的 reload() 会把它们
    # 重置成默认值（localhost / 空密码）—— fixture 的连接串不会自动传给它
    import os
    from urllib.parse import urlparse

    parsed = urlparse(os.environ["MINEWORKER_TEST_POSTGRES_URL"])

    postgres_db.execute("DROP TABLE IF EXISTS fullstack_items")
    postgres_db.execute("CREATE TABLE fullstack_items (url text primary key, title text)")

    with Site(n_items=N_ITEMS) as site:
        _configure(
            ITEM_PIPELINES=["mineworker.pipelines.postgres.PostgresPipeline"],
            ROBOTS_OBEY=False,
            POSTGRES_HOST=parsed.hostname or "127.0.0.1",
            POSTGRES_PORT=parsed.port or 5432,
            POSTGRES_USER=parsed.username or "postgres",
            POSTGRES_PASSWORD=parsed.password or "",
            POSTGRES_DB=(parsed.path or "/postgres").lstrip("/"),
        )
        DbSpider(site.url).start()

    rows = postgres_db.query("SELECT url, title FROM fullstack_items ORDER BY url")
    # N 个 item 页 + /gz + 重试成功的 /boom/429
    assert len(rows) == N_ITEMS + 2, f"库里只有 {len(rows)} 行：{rows}"
    titles = {r["title"] for r in rows}
    assert "gz" in titles and "ok429" in titles
