# 数据与去重

## Item

```python
class NewsItem(mw.Item):
    __table_name__ = "news"          # 不写则由类名推导：NewsItem -> news
    __unique_key__ = ["url"]         # 指纹只用这些字段（不写则用全部非空字段）

    def pre_to_db(self):             # 落库前钩子，可选
        self.title = self.title.strip()
```

```python
item = NewsItem()
item.url = "https://..."
item.title = "标题"
yield item
```

直接 `yield` 普通 `dict` 也行，落到 `ITEM_DEFAULT_TABLE`（默认 `items`），但不参与 Item 去重。

## Pipeline

`setting.py`：

```python
ITEM_PIPELINES = [
    "mineworker.pipelines.console.ConsolePipeline",
    "mineworker.pipelines.csv.CsvPipeline",
    "mineworker.pipelines.mongo.MongoPipeline",
]
```

| Pipeline | 说明 |
|---|---|
| `ConsolePipeline` | 打日志，调试用 |
| `CsvPipeline` | 按表写 `<CSV_OUTPUT_DIR>/<table>.csv`，首批数据决定表头 |
| `MongoPipeline` | `insert_many`；`UpdateItem` 按 `__update_key__` 逐条 `update_one` upsert |

自定义：继承 `mineworker.pipelines.base.BasePipeline`，实现 `save_items(table, items) -> bool`
（返回 `False` 该批会被 dump 到 `failed_items.jsonl`）。

单个 Item 可覆盖管道：`item.pipelines = ["myproj.pipelines.SpecialPipeline"]`。

## 去重

- **请求级**：`Request.filter_repeat=True` 时按 `fingerprint`（method + 规范化 URL + body）去重
- **Item 级**：`ITEM_FILTER_ENABLE=True` 时按 `Item.fingerprint` 去重，**写库成功后**才记指纹

```python
DEDUP_FILTER = "memory"   # 布隆过滤器，省内存，极小概率误判
DEDUP_FILTER = "lite"     # 精确 set，内存换准确
```

!!! note "关于「重跑不重复」"
    AirSpider 的去重是**每次运行新建的内存过滤器**，同进程重跑不会自动跳过。
    单机幂等的正确姿势是 `UpdateItem` + `__update_key__`（按键 upsert）。
    跨进程 / 断点续爬的持久化去重是 Redis 版 `Spider` 的能力（Roadmap）。

## 写库失败

某批 `save_items` 返回 `False` → dump 到 `failed_items.jsonl`，指纹不记。
恢复：`mineworker retry --items`（用当前 `ITEM_PIPELINES` 重放，仍失败的写回文件）。

## UpdateItem

```python
class PriceItem(mw.UpdateItem):
    __table_name__ = "price"
    __update_key__ = ["sku"]        # 按 sku 查已有记录，$set 更新，没有则插入
```
