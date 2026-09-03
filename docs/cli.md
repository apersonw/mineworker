# 命令行

需要 `pip install "mineworker[cli]"`。

## create

```bash
mineworker create -p news_crawler      # 项目脚手架
mineworker create -s ProductSpider     # 一个 AirSpider（写到 ./product_spider.py）
mineworker create -i ProductItem       # 一个 Item
mineworker create --setting            # 一份注释齐全的 setting.py
mineworker create -s Foo --force       # 覆盖已存在文件
```

名字会自动转换：`product-list` / `product_list` / `ProductList` 都能识别，
生成 `class ProductListSpider` + 文件 `product_list_spider.py`。

## shell

```bash
mineworker shell https://example.com
mineworker shell https://spa.example.com --render
```

抓一个页面进交互式 shell，绑定 `request` / `response` / `mw`。装了 IPython 用 IPython，
否则用内置 REPL（带 tab 补全）。

## retry

```bash
mineworker retry --items       # 重放 failed_items.jsonl 到当前 ITEM_PIPELINES
mineworker retry --requests    # 重新下载 failed_requests.jsonl 里的 URL
mineworker retry               # 两者都做
```

仍失败的记录写回文件，全部成功则删除文件。

## 版本

```bash
mineworker --version
```
