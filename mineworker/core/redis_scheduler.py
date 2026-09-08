"""``RedisScheduler`` —— 分布式调度器：Redis 队列 + Redis 去重 + 多节点结束检测。

多进程 / 多机跑同一个 Spider：队列 / 去重都在 Redis，天然断点续爬。
``start_requests`` 靠一次性锁保证只被某个节点执行一次；每个节点写心跳，
只有「所有活跃节点都空闲 + 队列空」时才判定结束（``keep_alive=True`` 则永不自停）。
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from mineworker import setting
from mineworker.core.base_scheduler import BaseScheduler
from mineworker.core.task_queue import RedisTaskQueue
from mineworker.db.redisdb import acquire_once, get_redis
from mineworker.dedup import get_request_filter
from mineworker.utils import stats as sk
from mineworker.utils import tools
from mineworker.utils.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from mineworker.core.base_parser import BaseParser
    from mineworker.dedup import Filter

log = get_logger("scheduler")

_FAILED_KEY = "failed_requests"


class _Heartbeat(threading.Thread):
    def __init__(self, redis: Any, hkey: str, node_id: str, pending_fn: Callable[[], int]) -> None:
        super().__init__(name="heartbeat", daemon=True)
        self._redis = redis
        self._hkey = hkey
        self._node_id = node_id
        self._pending_fn = pending_fn
        self._stop_event = threading.Event()

    def run(self) -> None:
        self._beat()
        while not self._stop_event.wait(setting.HEARTBEAT_INTERVAL):
            self._beat()

    def _beat(self) -> None:
        try:
            self._redis.hset(self._hkey, self._node_id, f"{time.time():.3f}:{self._pending_fn()}")
            self._redis.expire(self._hkey, max(2, int(setting.HEARTBEAT_STALE * 4)))
        except Exception:  # 心跳失败不该拖垮爬虫
            log.debug("心跳写入失败", exc_info=True)

    def stop(self) -> None:
        self._stop_event.set()


class RedisScheduler(BaseScheduler):
    def __init__(
        self,
        parser: BaseParser,
        *,
        redis_key: str,
        keep_alive: bool | None = None,
        **kwargs: Any,
    ) -> None:
        self._ns = f"{setting.REDIS_KEY_PREFIX}:{redis_key}"
        self._redis = get_redis()
        self._keep_alive = setting.SPIDER_KEEP_ALIVE if keep_alive is None else keep_alive
        self._node_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._hkey = f"{self._ns}:heartbeat"
        self._heartbeat: _Heartbeat | None = None
        #: 本节点是否拿到过任务 —— 启动宽限只保护「一个都没见过」的窗口
        self._seen_work = False
        self._started_at = time.monotonic()
        super().__init__(parser, **kwargs)

    # ------------------------------------------------------------------
    def _make_task_queue(self) -> RedisTaskQueue:
        return RedisTaskQueue(self._ns, self._redis)

    def _make_dedup(self) -> Filter:
        return get_request_filter(name=self._ns, redis_client=self._redis)

    def _on_start(self) -> None:
        self._heartbeat = _Heartbeat(self._redis, self._hkey, self._node_id, self._local_pending)
        self._heartbeat.start()
        log.info("节点 {} 加入（命名空间 {}）", self._node_id, self._ns)

    def _seed(self) -> None:
        if not acquire_once(self._redis, f"{self._ns}:lock:seed", ttl=setting.SPIDER_SEED_LOCK_TTL):
            log.info("另一节点已注入种子，本节点直接消费队列")
            return
        if not self._task_queue.empty():
            log.info("队列非空（{} 条），跳过种子注入，继续消费", self._task_queue.qsize())
            return
        log.info("种子请求 {} 条", self._seed_requests())

    def _is_done(self) -> bool:
        if self._keep_alive:
            return False
        if self._in_startup_grace():
            return False
        # 顺手回收租约到期的任务：结束检测本来就在周期性跑，不必另起线程。
        # 放在判定**之前**很关键 —— 否则「队列空了」会先成立、爬虫先退出，
        # 而那些被硬杀节点领走的任务还挂在在途表里没人捡
        if self._task_queue.reclaim_expired():
            return False
        if not self._local_idle() or not self._task_queue.empty():
            return False
        # 还有任务在别的节点手里没销账 —— 它们可能正在被处理，也可能属于一个
        # 已经死掉的节点。不能就此收工：租约到期后它们要被重新抓一遍
        if self._task_queue.inflight_count() > 0:
            return False
        return self._all_nodes_idle()

    def _in_startup_grace(self) -> bool:
        """本节点还在启动宽限期内、且**一个任务都没见过**。

        多节点同时启动时只有一个能拿到种子锁，其余节点看到的是空队列。没有这个
        宽限的话，它们会在 `DONE_CHECK_TIMES × DONE_CHECK_INTERVAL`（默认 1.5 秒）
        内判定「抓完了」直接退出 —— 而播种节点那时往往还没把种子推进队列。

        `_all_nodes_idle()` 挡不住这个：播种节点在那一刻的 pending 也是 0，
        它自己还没开始拉活，看上去和「闲着」没区别。

        只有从没拿到过任务的节点才等；拿到过活之后 `_seen_work` 为真，
        后续判定完全按原来的规则走，不会拖慢正常结束。
        """
        if self._seen_work:
            return False
        if self.stats.get(sk.REQUEST_OK) or self.stats.get(sk.REQUEST_FAILED):
            self._seen_work = True
            return False
        grace = setting.SPIDER_STARTUP_GRACE
        return grace > 0 and time.monotonic() - self._started_at < grace

    def _all_nodes_idle(self) -> bool:
        now = time.time()
        try:
            entries: dict[str, str] = self._redis.hgetall(self._hkey)
        except Exception:
            log.debug("读取心跳失败", exc_info=True)
            return False
        for raw in entries.values():
            ts_str, _, pending_str = raw.partition(":")
            try:
                if now - float(ts_str) > setting.HEARTBEAT_STALE:
                    continue  # 死节点
                if int(pending_str) > 0:
                    return False
            except ValueError:
                continue
        return True

    def _on_shutdown(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.stop()
            self._heartbeat.join(timeout=5)
        try:
            self._redis.hdel(self._hkey, self._node_id)
        except Exception:
            log.debug("清理心跳失败", exc_info=True)

        # 本节点采集器 / buffer 里没跑完的推回 Redis 队列，交给其他节点 / 重启接管
        leftovers = [
            *self._request_buffer.drain_pending(),
            *self._collector.drain(),
        ]
        for i, request in enumerate(leftovers):
            # 已经过过去重了；不清掉的话推回去也会被自己的指纹挡掉
            request.filter_repeat = False
            try:
                self._task_queue.put(request)
                # 推回队列之后要销掉旧租约，否则这条任务同时躺在队列和在途表里：
                # 别的节点抓完队列后 inflight_count() 仍大于 0，会一直不肯收工。
                # 顺序不能反 —— 先销账再入队的话，中间崩掉任务就两头都不在了
                self._task_queue.done(request)
            except Exception:
                # 这个函数**恰恰是在「Redis 出问题」时被调用的** —— 推不回去就落盘，
                # 否则剩下的请求静默消失：它们既不在队列里、也不在缓冲区里了
                log.exception("推回 Redis 失败，改为落盘")
                self._dump_requests(leftovers[i:], "退出时推回 Redis 失败")
                pushed = i
                break
        else:
            pushed = len(leftovers)
        if pushed:
            log.info("已把 {} 条未完成请求推回 Redis 队列", pushed)

    # ------------------------------------------------------------------
    def _on_failed_request(self, request: Any) -> None:
        """把重试耗尽的请求推到 Redis 失败列表。"""
        try:
            self._redis.rpush(f"{self._ns}:{_FAILED_KEY}", tools.dumps_json(request.to_dict()))
        except Exception:
            log.debug("写失败请求列表失败", exc_info=True)
