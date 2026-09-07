"""``MemoryBloomFilter`` —— 进程内布隆过滤器（bitarray）。

固定容量：按目标容量与误判率算好位数组大小和哈希个数。

**超容后误判率会急剧升高，而在去重里「误判」意味着一个从没抓过的 URL 被当成
「已抓过」静默丢掉。** 实测（容量 10 万、目标误判率 1e-6）：

===========  ==========  ====================
已插入        倍数        实测误判率
===========  ==========  ====================
100,000      1×          0%
300,000      3×          7.2%（每 13 个丢 1 个）
500,000      5×          53.7%（**一半抓不到**）
800,000      8×          92.8%
===========  ==========  ====================

所以超容时必须吵一声 —— 否则爬虫只是「提前结束了」或「少抓了很多」，
统计里只显示「去重 N 条」，看上去一切正常，查不出原因。
"""

from __future__ import annotations

import hashlib
import math
import threading

from bitarray import bitarray

from mineworker.utils.log import get_logger

log = get_logger("dedup")


def optimal_size(capacity: int, error_rate: float) -> int:
    size = math.ceil(-capacity * math.log(error_rate) / (math.log(2) ** 2))
    return max(size, 8)


def optimal_hashes(size: int, capacity: int) -> int:
    return max(1, round(size / capacity * math.log(2)))


def bit_positions(key: str, size: int, num_hashes: int) -> list[int]:
    """Kirsch-Mitzenmacher 双哈希，给出 key 在 size 位数组里要置位的下标。"""
    data = key.encode()
    h1 = int.from_bytes(hashlib.md5(data).digest(), "big")
    h2 = int.from_bytes(hashlib.sha1(data).digest(), "big") | 1
    return [(h1 + i * h2) % size for i in range(num_hashes)]


def warn_saturated(what: str, count: int, capacity: int) -> None:
    """超容告警。只发一次 —— 每插一条吵一次等于没吵。

    这条日志是有代价才加的：在此之前**完全没有任何信号**，`count` 有记录
    但没人读它。爬虫会静默地少抓一半站点而不报任何错。
    """
    log.warning(
        "{}已超出容量（{:,} / {:,}）—— 误判率正在急剧升高，"
        "新 URL 会被当成「已抓过」静默丢掉。"
        "调大 DEDUP_INITIAL_CAPACITY，或改用精确去重（DEDUP_FILTER=redis-set）",
        what,
        count,
        capacity,
    )


class MemoryBloomFilter:
    def __init__(self, capacity: int = 1_000_000, error_rate: float = 1e-6) -> None:
        if not 0 < error_rate < 1:
            raise ValueError("error_rate 必须在 (0, 1) 之间")
        self.capacity = max(1, capacity)
        self.error_rate = error_rate
        self.size = optimal_size(self.capacity, error_rate)
        self.num_hashes = optimal_hashes(self.size, self.capacity)
        self._bits = bitarray(self.size)
        self._bits.setall(0)
        self._lock = threading.Lock()
        self.count = 0
        self._warned = False

    def _positions(self, key: str) -> list[int]:
        return bit_positions(key, self.size, self.num_hashes)

    def add(self, key: str) -> bool:
        """返回 ``True`` 表示新指纹，``False`` 表示（几乎确定）已存在。"""
        positions = self._positions(key)
        with self._lock:
            if all(self._bits[p] for p in positions):
                return False
            for p in positions:
                self._bits[p] = 1
            self.count += 1
            saturated = self.count > self.capacity and not self._warned
            if saturated:
                self._warned = True
        if saturated:
            warn_saturated("内存布隆", self.count, self.capacity)
        return True

    def __contains__(self, key: str) -> bool:
        positions = self._positions(key)
        with self._lock:
            return all(self._bits[p] for p in positions)

    def __len__(self) -> int:
        return self.count


# ----------------------------------------------------------------------
def layer_params(
    index: int, capacity: int, error_rate: float, growth: float, ratio: float
) -> tuple[int, float]:
    """第 ``index`` 层的（容量, 误判率）。

    容量按 ``growth`` 倍增、误判率按 ``ratio`` 倍缩 —— 后者是关键：各层误判率
    构成等比数列，**总误判率收敛**到 ``error_rate / (1 - ratio)``。
    默认 ratio=0.5，所以无论加多少层，总误判率都不会超过目标值的 2 倍。
    """
    return max(1, int(capacity * growth**index)), error_rate * ratio**index


class ScalableBloomFilter:
    """分层布隆：一层填满就加一层，直到 ``max_layers`` 封顶。

    单层布隆的问题是**超容后静默失效**（见模块开头那张表）。分层把容量做上去，
    代价是内存：

    ===========  ==============  ===========  ==============
    层数          累计容量         累计内存      总误判率上界
    ===========  ==============  ===========  ==============
    1            1,000,000       3MB          1.0e-6
    4            15,000,000      57MB         1.88e-6
    6            63,000,000      260MB        1.97e-6
    8            255,000,000     1139MB       1.99e-6
    ===========  ==============  ===========  ==============

    **所以一定要有 ``max_layers``。** 让它无限长下去就是把内存变成无界资源 ——
    那正是 v4.4 刚从下载路径上清掉的东西，不该在这里原样请回来。
    到顶之后行为退化成单层布隆（继续往顶层塞、误判率升高），并沿用同一条超容告警。

    查询要遍历所有层：这是分层的固有代价，层数越多 ``__contains__`` 越慢，
    也是 ``max_layers`` 不宜开太大的另一个原因。
    """

    def __init__(
        self,
        capacity: int = 1_000_000,
        error_rate: float = 1e-6,
        *,
        max_layers: int = 4,
        growth: float = 2.0,
        ratio: float = 0.5,
    ) -> None:
        if not 0 < ratio < 1:
            raise ValueError("ratio 必须在 (0, 1) 之间，否则总误判率不收敛")
        if growth < 1:
            raise ValueError("growth 不能小于 1")
        self.base_capacity = max(1, capacity)
        self.error_rate = error_rate
        self.max_layers = max(1, max_layers)
        self.growth = growth
        self.ratio = ratio
        self._lock = threading.Lock()
        self._layers: list[MemoryBloomFilter] = [self._make_layer(0)]
        self._warned = False

    def _make_layer(self, index: int) -> MemoryBloomFilter:
        cap, err = layer_params(index, self.base_capacity, self.error_rate, self.growth, self.ratio)
        layer = MemoryBloomFilter(cap, err)
        # 层自己不告警：满了是**正常**的分层触发条件，吵的应该是整体到顶
        layer._warned = True
        return layer

    @property
    def capacity(self) -> int:
        """所有层的容量之和 —— 也就是到顶之前还能装多少。"""
        return sum(
            layer_params(i, self.base_capacity, self.error_rate, self.growth, self.ratio)[0]
            for i in range(self.max_layers)
        )

    @property
    def count(self) -> int:
        return sum(layer.count for layer in self._layers)

    def add(self, key: str) -> bool:
        with self._lock:
            if any(key in layer for layer in self._layers):
                return False
            top = self._layers[-1]
            if top.count >= top.capacity and len(self._layers) < self.max_layers:
                top = self._make_layer(len(self._layers))
                self._layers.append(top)
            top.add(key)
            saturated = (
                len(self._layers) >= self.max_layers
                and top.count > top.capacity
                and not self._warned
            )
            if saturated:
                self._warned = True
        if saturated:
            warn_saturated(f"分层布隆（已用满 {self.max_layers} 层）", self.count, self.capacity)
        return True

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return any(key in layer for layer in self._layers)

    def __len__(self) -> int:
        return self.count
