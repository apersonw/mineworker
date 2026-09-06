"""跨节点全局限速：把「该域下次可请求的时刻」放到 Redis 上算。

[`DomainThrottle`](throttle.py) 是**进程内**的 —— 分布式 Spider 起 N 个节点，
每个节点各自按 `DOWNLOAD_DELAY` 排队，目标站承受的就是 N 倍。这个模块让所有
节点共用一份 `next_at`，于是 N 个节点合起来才是配置的那个速率。

算法和本地版是同一个（GCRA / 虚拟调度，也就是本地那个「取号」）：
取号者拿走 ``max(now, next_at)`` 作为起跑时刻，并把 `next_at` 推到
``起跑时刻 + delay``。差别只在这一步必须**原子**完成，所以用 Lua。

一次往返就能算出准确的等待时长 —— 不需要「没令牌就 sleep 一下再试」的轮询，
那种写法在高并发下会把 Redis 打成热点，而且等待时长只能靠猜。

**时钟取自 Redis 服务端**（``TIME``）而不是各节点的 `time.time()`：
节点间的时钟偏移会一比一变成限速误差，而所有节点看的是同一个 Redis。

因此要求 **Redis 5+**：脚本在写入前调了 `TIME`（非确定性命令），
Redis 5 起脚本改走 effects 复制才允许这么写。更老的服务端会报错，
届时按 `take_ticket` 的兜底逻辑退回进程内限速。
"""

from __future__ import annotations

import threading

from mineworker.utils.log import get_logger

log = get_logger("throttle")

#: 取号：读 next_at → 算起跑时刻 → 推进 next_at → 返回要等多久（毫秒）。
#:
#: penalize 用的也是这个脚本：把「间隔」换成「惩罚秒数」，算式完全一样，
#: 区别只是调用方不关心返回值。本地版同理（`_take_ticket` 与 `penalize` 同构）。
_TICKET_LUA = """
local key = KEYS[1]
local delay = tonumber(ARGV[1])
local t = redis.call('TIME')
local now = t[1] * 1000 + t[2] / 1000
local next_at = tonumber(redis.call('GET', key) or '0')
local ready = now
if next_at > now then ready = next_at end
local target = ready + delay
-- TTL 跟着预约走：key 必须活得比最后一张号的起跑时刻更久，否则预约会凭空蒸发。
-- 多给 60 秒余量，同时保证闲置的域会自己过期，不留垃圾 key。
redis.call('SET', key, math.floor(target), 'PX', math.floor(target - now) + 60000)
return math.floor(ready - now)
"""


class GlobalThrottle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._warned = False

    def take_ticket(self, domain: str, delay: float) -> float | None:
        """取号，返回需要等待的秒数。

        Redis 不可用时返回 ``None``，调用方据此退回本地限速 —— 注意这是
        **fail-safe 而不是 fail-open**：退回去的是进程内限速，不是「不限速」。
        限速器连不上就放开手脚打目标站，是最不该有的失败模式。
        """
        try:
            # 惰性导入：`redisdb` 顶层 import redis，而 redis 是可选 extra。
            # 放到模块顶层会让没装 `mineworker[redis]` 的用户连 throttle 都 import 不了 ——
            # 而 throttle 在核心路径上（parser_control 每个请求都走）
            from mineworker.db import redisdb

            client = redisdb.get_redis()
            # 不缓存 Script 对象：`register_script` 是纯本地的（只算一次 sha1，
            # 不走网络），而按 client 缓存要拿 id() 当键 —— close_redis() 之后
            # id 可能被新对象复用，取到绑在已关闭 client 上的旧 Script，
            # 结果是全局限速永久静默退回本地。省不下什么，却多一种坏法
            wait_ms = client.register_script(_TICKET_LUA)(
                keys=[redisdb.key("throttle", domain)],
                args=[max(delay, 0.0) * 1000],
            )
        except Exception as exc:
            if not self._warned:
                self._warned = True
                log.warning("全局限速不可用，退回进程内限速：{}", exc)
            return None
        return max(float(wait_ms) / 1000, 0.0)

    def reset(self) -> None:
        with self._lock:
            self._warned = False


#: 进程级单例
_default = GlobalThrottle()


def take_ticket(domain: str, delay: float) -> float | None:
    return _default.take_ticket(domain, delay)


def reset() -> None:
    _default.reset()
