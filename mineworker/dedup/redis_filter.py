"""Redis 去重过滤器：布隆（省空间）与精确 set。

多进程 / 多节点共享同一份去重状态，配合分布式 Spider 实现「断点续爬不重复」。
"""

from __future__ import annotations

from typing import Any

from mineworker import setting
from mineworker.dedup.bloom_filter import (
    bit_positions,
    layer_params,
    optimal_hashes,
    optimal_size,
    warn_saturated,
)


class RedisSetFilter:
    """精确去重：一个 Redis SET，成员即指纹。不会误判，但内存随量线性增长。"""

    def __init__(self, name: str, redis_client: Any = None) -> None:
        self._r: Any = redis_client if redis_client is not None else _default_redis()
        self._key = f"{name}:dedup:set"

    def add(self, key: str) -> bool:
        return bool(self._r.sadd(self._key, key))

    def __contains__(self, key: str) -> bool:
        return bool(self._r.sismember(self._key, key))

    def __len__(self) -> int:
        return int(self._r.scard(self._key))


#: 查所有层 + 往顶层置位 + 计数，**一次往返且原子**。
#:
#: 原来是 pipeline 的 SETBIT —— pipeline 只是打包发送，并不保证原子，所以存在
#: 跨节点竞态（两个节点同时判定为「新」）。改成脚本后这个竞态一并消失，
#: 计数也才可能准。
#:
#: **层数由共享计数器确定性推导**：每个节点拿同一个 count 算出同一个「当前顶层」，
#: 不需要任何协调。边界处的竞争无害 —— 一个 key 落在第 L 层还是 L+1 层都行，
#: 因为读的时候 1..top 全查。
#:
#: KEYS[1]=计数器，KEYS[2..]=各层位数组（低层在前）
#: ARGV[1]=层数；随后每层依次是：累计容量、哈希个数、各哈希位置
_ADD_LUA = """
local counter = KEYS[1]
local n = tonumber(redis.call('GET', counter) or '0')
local nl = tonumber(ARGV[1])
local idx = 2
local cum, poss = {}, {}
for l = 1, nl do
  cum[l] = tonumber(ARGV[idx]); idx = idx + 1
  local k = tonumber(ARGV[idx]); idx = idx + 1
  local p = {}
  for j = 1, k do p[j] = ARGV[idx]; idx = idx + 1 end
  poss[l] = p
end
local top = nl
for l = 1, nl do
  if n < cum[l] then top = l; break end
end
for l = 1, top do
  local all = true
  for j = 1, #poss[l] do
    if redis.call('GETBIT', KEYS[l + 1], poss[l][j]) == 0 then all = false; break end
  end
  if all then return {0, n} end
end
for j = 1, #poss[top] do
  redis.call('SETBIT', KEYS[top + 1], poss[top][j], 1)
end
return {1, redis.call('INCR', counter)}
"""


class RedisBloomFilter:
    """Redis 位数组做的分层布隆。多节点共享同一份状态。

    单层布隆超容后会**静默失效**（新 URL 被当成抓过丢掉，实测 5 倍容量时丢一半）。
    这里一层填满就加一层，直到 ``max_layers`` 封顶 —— 封顶是刻意的，
    让层数无限长下去就是把 Redis 内存变成无界资源。各层成本见
    :class:`~mineworker.dedup.bloom_filter.ScalableBloomFilter` 的表。

    **第 0 层沿用老 key**（``{name}:dedup:bloom``），所以升级上来的已有去重状态
    不会失效 —— 否则所有人的断点续爬都会从头再抓一遍。
    """

    def __init__(
        self,
        name: str,
        redis_client: Any = None,
        *,
        capacity: int = 1_000_000,
        error_rate: float = 1e-6,
        max_layers: int | None = None,
        growth: float = 2.0,
        ratio: float = 0.5,
    ) -> None:
        if not 0 < error_rate < 1:
            raise ValueError("error_rate 必须在 (0, 1) 之间")
        if not 0 < ratio < 1:
            raise ValueError("ratio 必须在 (0, 1) 之间，否则总误判率不收敛")
        self._r: Any = redis_client if redis_client is not None else _default_redis()
        self._base = f"{name}:dedup:bloom"
        self._count_key = f"{name}:dedup:bloom:count"
        self.base_capacity = max(1, capacity)
        self.error_rate = error_rate
        self.max_layers = max(1, setting.DEDUP_MAX_LAYERS if max_layers is None else max_layers)
        self._warned = False

        self._layer_keys: list[str] = []
        self._layer_shape: list[tuple[int, int]] = []  # (size, num_hashes)
        self._cumulative: list[int] = []
        total = 0
        for i in range(self.max_layers):
            cap_i, err_i = layer_params(i, self.base_capacity, error_rate, growth, ratio)
            size = optimal_size(cap_i, err_i)
            self._layer_keys.append(self._base if i == 0 else f"{self._base}:{i}")
            self._layer_shape.append((size, optimal_hashes(size, cap_i)))
            total += cap_i
            self._cumulative.append(total)
        # 兼容老代码：单层时 size / num_hashes 就是第 0 层的
        self.size, self.num_hashes = self._layer_shape[0]

    @property
    def capacity(self) -> int:
        """所有层的容量之和 —— 到顶之前还能装多少。"""
        return self._cumulative[-1]

    @property
    def count(self) -> int:
        """已插入的指纹数（**跨节点共享**，每个节点各数各的等于没数）。"""
        return int(self._r.get(self._count_key) or 0)

    def _args(self, key: str) -> list[Any]:
        args: list[Any] = [len(self._layer_keys)]
        for i, (size, num_hashes) in enumerate(self._layer_shape):
            positions = bit_positions(key, size, num_hashes)
            args.append(self._cumulative[i])
            args.append(len(positions))
            args.extend(positions)
        return args

    def add(self, key: str) -> bool:
        # 不缓存 Script 对象：register_script 是纯本地的（只算 sha1，不走网络），
        # 而按 id(client) 缓存会在 close_redis() 后拿到绑在已关闭 client 上的旧对象
        fresh, count = self._r.register_script(_ADD_LUA)(
            keys=[self._count_key, *self._layer_keys], args=self._args(key)
        )
        if count > self.capacity and not self._warned:
            self._warned = True
            warn_saturated(f"Redis 分层布隆（{self.max_layers} 层已满）", int(count), self.capacity)
        return bool(fresh)

    def __contains__(self, key: str) -> bool:
        pipe = self._r.pipeline()
        for i, (size, num_hashes) in enumerate(self._layer_shape):
            for pos in bit_positions(key, size, num_hashes):
                pipe.getbit(self._layer_keys[i], pos)
        results = pipe.execute()
        at = 0
        for _, num_hashes in self._layer_shape:
            if all(results[at : at + num_hashes]):
                return True
            at += num_hashes
        return False


def _default_redis() -> Any:
    from mineworker.db.redisdb import get_redis

    return get_redis()
