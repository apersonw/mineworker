# examples

可以直接跑的完整示例。

```bash
pip install mineworker
python examples/books_toscrape.py
```

| 示例 | 演示 |
|---|---|
| [`books_toscrape.py`](books_toscrape.py) | 两级抓取（列表页翻页 → 详情页）、`cb_kwargs` 传状态、`urljoin`、`Item` + `__unique_key__` 去重、礼貌性设置 |

## 关于目标站点

示例抓的是 [books.toscrape.com](https://books.toscrape.com/) ——
Zyte 专门为爬虫练习搭建的站点，可以放心跑。

**换成你自己的目标站之前**，先读一遍[反爬对抗](https://apersonw.github.io/mineworker/anti-bot/)
和[限速](https://apersonw.github.io/mineworker/spider/#_5)：
默认配置对练习站够用，对真实站点通常需要按对方承受能力重新调。

## 示例里那几行礼貌性设置

```python
setting.ROBOTS_OBEY = True  # 遵守 robots.txt
setting.CONCURRENT_REQUESTS_PER_DOMAIN = 4  # 单域最多 4 个在途
setting.DOWNLOAD_DELAY = 0.3  # 同域两次请求至少隔 0.3s
```

⚠️ 这套限速是**进程内**的。分布式起 N 个节点，目标站承受的就是 N 倍。
