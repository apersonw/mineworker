"""Redis 去重过滤器：布隆（省空间）与精确 set。

多进程 / 多节点共享同一份去重状态，配合分布式 Spider 实现「断点续爬不重复」。
"""

from __future__ import annotations

from typing import Any

from mineworker.dedup.bloom_filter import bit_positions, optimal_hashes, optimal_size


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


class RedisBloomFilter:
    """Redis 字符串当位数组，SETBIT/GETBIT。固定容量，超出后误判率上升。

    ``add`` 用一次 pipeline 的 SETBIT 拿到旧值判断新旧：存在跨节点竞态（两个节点
    同时判定为「新」），对去重来说只是偶尔多处理一次，可接受。
    """

    def __init__(
        self,
        name: str,
        redis_client: Any = None,
        *,
        capacity: int = 1_000_000,
        error_rate: float = 1e-6,
    ) -> None:
        if not 0 < error_rate < 1:
            raise ValueError("error_rate 必须在 (0, 1) 之间")
        self._r: Any = redis_client if redis_client is not None else _default_redis()
        self._key = f"{name}:dedup:bloom"
        self.size = optimal_size(max(1, capacity), error_rate)
        self.num_hashes = optimal_hashes(self.size, max(1, capacity))

    def add(self, key: str) -> bool:
        positions = bit_positions(key, self.size, self.num_hashes)
        pipe = self._r.pipeline()
        for pos in positions:
            pipe.setbit(self._key, pos, 1)
        old_bits = pipe.execute()
        return not all(old_bits)

    def __contains__(self, key: str) -> bool:
        positions = bit_positions(key, self.size, self.num_hashes)
        pipe = self._r.pipeline()
        for pos in positions:
            pipe.getbit(self._key, pos)
        return all(pipe.execute())


def _default_redis() -> Any:
    from mineworker.db.redisdb import get_redis

    return get_redis()
