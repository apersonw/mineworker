"""任务队列。

- :class:`MemoryTaskQueue` —— 单进程，``queue.PriorityQueue``
- :class:`RedisTaskQueue`  —— Redis zset（score=priority），多进程 / 多节点共享，
  支持断点续爬；接口与内存版一致

两者都提供 ``put`` / ``get`` / ``qsize`` / ``empty``；Redis 版额外提供 ``get_batch``。
"""

from __future__ import annotations

import itertools
import queue
import time
from typing import TYPE_CHECKING, Any

from mineworker import setting
from mineworker.utils import tools
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from mineworker.network.request import Request


class MemoryTaskQueue:
    def __init__(self) -> None:
        self._q: queue.PriorityQueue[tuple[int, int, Request]] = queue.PriorityQueue()
        self._seq = itertools.count()

    def put(self, request: Request) -> None:
        self._q.put((request.priority, next(self._seq), request))

    def get(self, timeout: float | None = None) -> Request | None:
        try:
            return self._q.get(timeout=timeout)[2]
        except queue.Empty:
            return None

    def get_batch(self, count: int) -> list[Request]:
        out: list[Request] = []
        for _ in range(max(1, count)):
            try:
                out.append(self._q.get_nowait()[2])
            except queue.Empty:
                break
        return out

    def qsize(self) -> int:
        return self._q.qsize()

    def empty(self) -> bool:
        return self._q.empty()


log = get_logger("queue")

#: 把租约到期的在途任务搬回队列，**原子**。
#:
#: 先 zrem 再 zadd 分两步做的话，中间崩掉任务就两头都不在了 ——
#: 那比不回收还糟。
_RECLAIM_LUA = """
local inflight, queue = KEYS[1], KEYS[2]
local expired = redis.call('ZRANGEBYSCORE', inflight, '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
for i = 1, #expired do
  local member = expired[i]
  redis.call('ZREM', inflight, member)
  -- 放回队列头部（score 0）：这些任务已经等了一个租约周期，不该再排到最后
  redis.call('ZADD', queue, 0, member)
end
return #expired
"""


#: 取任务 + 记租约，**一次往返且原子**。
#:
#: 队列用 `zpopmin` —— 取走即删。任务一旦进了某个节点的内存，Redis 里就不存在了；
#: 进程被 SIGKILL（OOM Killer / 断电 / docker kill）硬杀，它们就随进程一起消失，
#: 没有任何一方还记得它们。实测 24 个任务被杀后只剩 5 个。
#:
#: 所以取走的同时把任务记进「在途」zset，score = 租约到期时刻。
#: **这两步必须原子**：只取走没记账的话，任务会丢得比现在更隐蔽 ——
#: 现在至少是整批一起丢，那样是随机零星丢。
_TAKE_LUA = """
local queue, inflight = KEYS[1], KEYS[2]
-- 直接用 ARGV 里的原始字符串，不要 tonumber 之后再传回去：
-- Lua 的 number 都是浮点，把 count 传成 100.0 会被 redis 拒绝
local rows = redis.call('ZPOPMIN', queue, ARGV[1])
local lease_on = tonumber(ARGV[2]) > 0
local out = {}
-- ⚠️ 两种回包形状都要认：真 Redis 返回**扁平**数组 {member, score, member, score}，
-- 而 fakeredis 返回**嵌套**的 {{member, score}, ...}。只按一种写，要么单测挂、
-- 要么线上挂 —— 实测 #rows 真 Redis=4 / fakeredis=2，rows[1] 一个是 string 一个是 table。
if type(rows[1]) == 'table' then
  for i = 1, #rows do
    out[#out + 1] = rows[i][1]
  end
else
  for i = 1, #rows, 2 do
    out[#out + 1] = rows[i]
  end
end
if lease_on then
  for i = 1, #out do
    redis.call('ZADD', inflight, ARGV[2], out[i])
  end
end
return out
"""


class RedisTaskQueue:
    def __init__(self, name: str, redis_client: Any = None) -> None:
        self._r: Any = redis_client if redis_client is not None else _default_redis()
        self._key = f"{name}:z_requests"
        self._inflight_key = f"{name}:z_inflight"

    def put(self, request: Request) -> None:
        payload = tools.dumps_json(request.to_dict())
        self._r.zadd(self._key, {payload: request.priority})

    def _take(self, count: int) -> list[Request]:
        """取至多 ``count`` 个任务，并把它们记进在途表。"""
        lease = setting.SPIDER_TASK_LEASE
        deadline = (time.time() + lease) if lease > 0 else 0
        members = self._r.register_script(_TAKE_LUA)(
            keys=[self._key, self._inflight_key], args=[str(max(1, int(count))), f"{deadline:.3f}"]
        )
        out: list[Request] = []
        for member in members:
            request = _decode(member)
            if request is None:
                # 解不出来的成员直接销账，否则它会一直卡在在途表里反复被归还
                self.done_member(member)
                continue
            # 记住**原始 payload**：`retry_times` 等字段在处理过程中会变，
            # 重新序列化得到的字符串和当初存进去的那条对不上，销账就会失败
            request.lease_token = member
            out.append(request)
        return out

    def get(self, timeout: float | None = None) -> Request | None:
        # 先试非阻塞的一次 —— 有活就立刻走原子取用路径
        taken = self._take(1)
        if taken:
            return taken[0]
        if not timeout:
            return None
        # 队列空：用 bzpopmin 阻塞等，拿到后补记租约。这里没法做成一步原子，
        # 但窗口只有「拿到之后、记账之前」这一瞬，且只影响单条
        popped = self._r.bzpopmin(self._key, timeout=timeout)
        if not popped:
            return None
        member = popped[1]
        request = _decode(member)
        if request is None:
            return None
        lease = setting.SPIDER_TASK_LEASE
        if lease > 0:
            self._r.zadd(self._inflight_key, {member: time.time() + lease})
        request.lease_token = member
        return request

    def get_batch(self, count: int) -> list[Request]:
        return self._take(count)

    def done(self, request: Request) -> None:
        """任务处理完（成功或彻底失败），销账。"""
        token = getattr(request, "lease_token", None)
        if token:
            self.done_member(token)

    def reclaim_expired(self, limit: int = 500) -> int:
        """把租约到期的在途任务放回队列，返回归还条数。

        节点被 SIGKILL 硬杀时不会销账，这些任务会一直挂在在途表里 ——
        本方法是它们唯一的回收途径（优雅停止和退出落盘都要求进程还活着）。

        ⚠️ **这带来「至少一次」语义**：节点只是卡住（长 GC、慢下载）而不是死了的话，
        任务会被归还并再处理一遍。分布式队列绕不开这个取舍，请求去重能挡住大部分
        重复入库，但回调仍可能跑两次 —— 副作用要自己保证幂等。

        搬运用 Lua 做成原子：先 zrem 再 zadd 的话，中间崩了任务就两头都不在了。
        """
        if setting.SPIDER_TASK_LEASE <= 0:
            return 0
        try:
            moved = self._r.register_script(_RECLAIM_LUA)(
                keys=[self._inflight_key, self._key],
                args=[f"{time.time():.3f}", str(max(1, int(limit)))],
            )
        except Exception:
            log.debug("回收过期任务失败", exc_info=True)
            return 0
        n = int(moved)
        if n:
            log.warning("有 {} 个任务的租约到期，已放回队列（节点可能被硬杀了）", n)
        return n

    def inflight_count(self) -> int:
        return int(self._r.zcard(self._inflight_key))

    def done_member(self, member: str) -> None:
        try:
            self._r.zrem(self._inflight_key, member)
        except Exception:
            log.debug("销账失败，任务会在租约到期后被重新领取", exc_info=True)

    def qsize(self) -> int:
        return int(self._r.zcard(self._key))

    def empty(self) -> bool:
        return self.qsize() == 0


def _decode(member: Any) -> Request | None:
    if member is None:
        return None
    from mineworker.network.request import Request

    return Request.from_dict(tools.loads_json(member))


def _default_redis() -> Any:
    from mineworker.db.redisdb import get_redis

    return get_redis()
