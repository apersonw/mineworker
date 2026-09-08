"""响应缓存：开发调试期反复重跑爬虫时，不必每次都去打目标站。

写爬虫是个来回试的过程 —— 改一版选择器、重跑一遍看结果，一天下来同一批页面
可能被抓几十遍。这既慢，对目标站也不礼貌。打开缓存后第一遍照常抓，之后重跑
直接读本地文件。

**默认关闭，而且只在明确打开时才生效** —— 缓存跑生产是很容易出事的：你以为在
抓新数据，其实读的是几天前的副本。所以这个开关的定位就是「开发期工具」。

用文件而不是 Redis：主要场景是本机调 `AirSpider`，那时候未必有 Redis 在跑。

缓存命中时**不占限速名额、也不发请求** —— 钩子挂在 `download_request()` 里、
限速之前。（robots.txt 检查在更外层，仍会走，但它本身按域缓存，最多一次请求。）
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from mineworker import setting
from mineworker.network import status as status_policy
from mineworker.utils import tools
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.network.request import Request
    from mineworker.network.response import Response

log = get_logger("cache")


def _entry_path(request: Request) -> Path:
    # 指纹已经包含 method + 规范化 URL + body，直接拿来当文件名
    fp = request.fingerprint
    root = Path(setting.RESPONSE_CACHE_PATH)
    # 按前两位分子目录：一次大爬取几十万个文件堆在同一层，`ls` 都会卡住
    return root / fp[:2] / f"{fp}.json"


def _cacheable(request: Request) -> bool:
    """只缓存 GET。

    POST / PUT 不是幂等的，把它们的响应缓存下来重放，等于把一次写操作的结果
    当成了「这个请求现在会返回什么」—— 这两件事根本不是一回事。
    """
    return request.method == "GET"


def load(request: Request) -> Response | None:
    """取缓存；没有、过期或读坏了都返回 ``None``（调用方照常去抓）。"""
    if not setting.RESPONSE_CACHE_ENABLE or not _cacheable(request):
        return None
    path = _entry_path(request)
    try:
        if not path.is_file():
            return None
        expire = setting.RESPONSE_CACHE_EXPIRE
        if expire > 0 and time.time() - path.stat().st_mtime > expire:
            return None
        from mineworker.network.response import Response

        response = Response.from_dict(tools.loads_json(path.read_text(encoding="utf-8")))
    except Exception:
        # 缓存是加速手段，不该成为新的失败源：读坏了就当没有
        log.debug("读取响应缓存失败：{}", request.url, exc_info=True)
        return None
    # 回调里会用 response.request 拿 cb_kwargs，反序列化时补回来
    response.request = request
    log.debug("缓存命中：{}", request.url)
    return response


def store(request: Request, response: Response) -> None:
    """写缓存。失败只记 debug —— 抓取本身已经成功了，不该因为写文件失败而失败。"""
    if not setting.RESPONSE_CACHE_ENABLE or not _cacheable(request):
        return
    # 只缓存「正常」的响应：把 429 / 503 存下来重放，等于每次重跑都在读那张
    # 限速页，还会让状态码策略以为站点一直在拒绝
    if status_policy.classify(response) != "ok":
        return
    path = _entry_path(request)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再改名：中途崩了不会留下半个 JSON，下次读到就是坏数据
        tmp = path.with_suffix(".part")
        tmp.write_text(tools.dumps_json(response.to_dict()), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        log.debug("写入响应缓存失败：{}", request.url, exc_info=True)


def clear() -> int:
    """清空缓存目录，返回删掉的条目数。``mineworker cache --clear`` 用。"""
    root = Path(setting.RESPONSE_CACHE_PATH)
    if not root.is_dir():
        return 0
    n = 0
    for entry in root.rglob("*.json"):
        entry.unlink(missing_ok=True)
        n += 1
    return n
