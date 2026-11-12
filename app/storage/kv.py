"""Redis 键值存储（redis.asyncio）。

服务不可达时降级为进程内字典实现（接口一致），
用于：任务状态 task:{id}:status / agent:{name}:heartbeat / LLM 缓存。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import redis.asyncio as aioredis

from app import config

logger = logging.getLogger(__name__)


class _MemoryKV:
    """进程内降级实现，提供与 redis.asyncio 相同的最小接口。"""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._expire_at: dict[str, float] = {}

    def _alive(self, key: str) -> bool:
        exp = self._expire_at.get(key)
        if exp is not None and exp < time.time():
            self._data.pop(key, None)
            self._expire_at.pop(key, None)
            return False
        return key in self._data

    async def get(self, key: str) -> str | None:
        return self._data.get(key) if self._alive(key) else None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value
        if ex:
            self._expire_at[key] = time.time() + ex

    async def delete(self, *keys: str) -> None:
        for k in keys:
            self._data.pop(k, None)
            self._expire_at.pop(k, None)

    async def keys(self, pattern: str) -> list[str]:
        import fnmatch

        return [k for k in list(self._data) if self._alive(k) and fnmatch.fnmatch(k, pattern)]

    async def close(self) -> None:  # pragma: no cover
        return None


class KVStore:
    """统一的异步 KV 接口：Redis 优先，失败降级内存。"""

    def __init__(self) -> None:
        self._redis: Any | None = None
        self._memory: _MemoryKV | None = None
        self._degraded = False  # Redis 运行时故障后置位，只提示一次

    def _fallback(self) -> _MemoryKV:
        """运行时故障的兜底：保证刚写入的状态不会"写完就丢"。"""
        if self._memory is None:
            self._memory = _MemoryKV()
        if not self._degraded:
            self._degraded = True
            logger.warning(
                "[KV] Redis 运行时不可用，本次运行降级为进程内存（状态仍可读，"
                "但重启后丢失；前端时间线不受影响）"
            )
        return self._memory

    async def connect(self) -> None:
        if config.FORCE_MEMORY_KV or not config.REDIS_ENABLED:
            self._memory = _MemoryKV()
            logger.warning("[KV] Redis 已禁用，使用进程内存降级实现")
            return
        try:
            client = aioredis.from_url(
                config.REDIS_URL, socket_connect_timeout=1.0, decode_responses=True
            )
            await client.ping()
            self._redis = client
            logger.info("[KV] Redis 已连接: %s", config.REDIS_URL)
        except Exception as exc:  # noqa: BLE001
            self._memory = _MemoryKV()
            logger.warning("[KV] Redis 不可达 (%s)，降级为进程内存实现", exc)

    @property
    def is_redis(self) -> bool:
        return self._redis is not None

    async def get(self, key: str) -> str | None:
        if self._redis is not None and not self._degraded:
            try:
                return await self._redis.get(key)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[KV] Redis get 失败: %s", exc)
                return await self._fallback().get(key)
        if self._redis is not None:
            return await self._fallback().get(key)
        assert self._memory is not None
        return await self._memory.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        if self._redis is not None and not self._degraded:
            try:
                await self._redis.set(key, value, ex=ttl)
                return
            except Exception as exc:  # noqa: BLE001
                # 单次抖动不应丢数据：同时写内存兜底，下次读还能读到
                logger.debug("[KV] Redis set 失败: %s", exc)
                await self._fallback().set(key, value, ex=ttl)
                return
        if self._redis is not None:
            await self._fallback().set(key, value, ex=ttl)
            return
        assert self._memory is not None
        await self._memory.set(key, value, ex=ttl)

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass


# 任务状态键约定（与架构文档一致）
def task_status_key(task_id: str) -> str:
    return f"task:{task_id}:status"


def heartbeat_key(agent_name: str) -> str:
    return f"agent:{agent_name}:heartbeat"


def llm_cache_key(prompt_hash: str) -> str:
    return f"llm:cache:{prompt_hash}"
