"""``MemoryBloomFilter`` —— 进程内布隆过滤器（bitarray）。

固定容量：按目标容量与误判率算好位数组大小和哈希个数。超出容量后误判率会升高，
海量 / 分布式场景应换用 Roadmap v2 的 Redis 可扩展布隆。
"""

from __future__ import annotations

import hashlib
import math
import threading

from bitarray import bitarray


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
            return True

    def __contains__(self, key: str) -> bool:
        positions = self._positions(key)
        with self._lock:
            return all(self._bits[p] for p in positions)

    def __len__(self) -> int:
        return self.count
