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
        return self._local_idle() and self._task_queue.empty() and self._all_nodes_idle()

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
        for request in leftovers:
            request.filter_repeat = False
            self._task_queue.put(request)
        if leftovers:
            log.info("已把 {} 条未完成请求推回 Redis 队列", len(leftovers))

    # ------------------------------------------------------------------
    def _on_failed_request(self, request: Any) -> None:
        """把重试耗尽的请求推到 Redis 失败列表。"""
        try:
            self._redis.rpush(f"{self._ns}:{_FAILED_KEY}", tools.dumps_json(request.to_dict()))
        except Exception:
            log.debug("写失败请求列表失败", exc_info=True)
