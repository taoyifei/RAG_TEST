"""进程内、实际 route 绑定的 P07 最终结果缓存。"""

from __future__ import annotations

import threading
import time
from bisect import bisect_left, insort
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from rag_app.core.models import SearchAnswerResult

_DEFAULT_MAX_ENTRIES = 512
_DEFAULT_MAX_APPROX_BYTES = 64 * 1024 * 1024
_DEFAULT_PRUNE_EVERY_OPERATIONS = 64
_DEFAULT_PRUNE_BATCH_SIZE = 16
_ENTRY_OVERHEAD_BYTES = 128


@dataclass(frozen=True, slots=True)
class RetrievalCacheMetrics:
    """不包含缓存键或正文的进程内缓存指标快照。"""

    entries: int
    approx_bytes: int
    hits: int
    misses: int
    evictions: int
    expired: int


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """同时服务 LRU、TTL 和容量核算的单条缓存记录。"""

    result: SearchAnswerResult
    expires_at: float
    approx_bytes: int
    sequence: int


class InMemoryRetrievalCache:
    """不持久化正文，按 TTL、LRU 和容量上限约束进程内结果。"""

    def __init__(
        self,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        max_approx_bytes: int | None = _DEFAULT_MAX_APPROX_BYTES,
        prune_every_operations: int = _DEFAULT_PRUNE_EVERY_OPERATIONS,
        prune_batch_size: int = _DEFAULT_PRUNE_BATCH_SIZE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """配置硬容量边界和低频到期清理预算。

        Args:
            max_entries: 可同时保留的最大结果数。
            max_approx_bytes: key 与完整结果 JSON 的估算字节上限；None
                表示只使用条目数上限。
            prune_every_operations: 每多少次 get/put 触发一次低频清理。
            prune_batch_size: 单次低频或显式清理最多移除的到期条目数。
            clock: 单调时钟；允许测试注入。

        Raises:
            ValueError: 任一容量或清理参数不是正数。

        """
        if max_entries <= 0:
            raise ValueError("Retrieval cache max_entries 必须为正数。")
        if max_approx_bytes is not None and max_approx_bytes <= 0:
            raise ValueError(
                "Retrieval cache max_approx_bytes 必须为正数或 None。"
            )
        if prune_every_operations <= 0:
            raise ValueError(
                "Retrieval cache prune_every_operations 必须为正数。"
            )
        if prune_batch_size <= 0:
            raise ValueError("Retrieval cache prune_batch_size 必须为正数。")
        self._max_entries = max_entries
        self._max_approx_bytes = max_approx_bytes
        self._prune_every_operations = prune_every_operations
        self._prune_batch_size = prune_batch_size
        self._clock = clock
        self._values: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._expirations: list[tuple[float, int, str]] = []
        self._lock = threading.Lock()
        self._closed = False
        self._approx_bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expired = 0
        self._operations_since_prune = 0
        self._sequence = 0

    def get(self, cache_key: str) -> SearchAnswerResult | None:
        """读取完整 SHA-256 key 对应结果。

        Args:
            cache_key: 已绑定实际 route 和 rerank mode 的缓存键。

        Returns:
            命中的最终结果，否则为 None。

        """
        with self._lock:
            self._ensure_open_locked()
            now = self._clock()
            self._maybe_prune_locked(now)
            cached = self._values.get(cache_key)
            if cached is None:
                self._misses += 1
                return None
            if cached.expires_at <= now:
                self._discard_locked(cache_key, expired=True)
                self._misses += 1
                return None
            self._values.move_to_end(cache_key)
            self._hits += 1
            return cached.result

    def put(
        self,
        cache_key: str,
        result: SearchAnswerResult,
        *,
        ttl_seconds: int = 300,
    ) -> None:
        """保存已通过 confidence/citation 门的最终结果。

        Args:
            cache_key: 已绑定实际执行结果的缓存键。
            result: 已通过发布门的统一查询结果。
            ttl_seconds: 正数缓存生命周期。

        Returns:
            无返回值。

        """
        if ttl_seconds <= 0:
            raise ValueError("Retrieval cache TTL 必须为正数。")
        if result.cache_key != cache_key:
            raise ValueError("Retrieval cache key 与结果身份不一致。")
        approx_bytes = _entry_approx_bytes(cache_key, result)
        with self._lock:
            self._ensure_open_locked()
            now = self._clock()
            self._maybe_prune_locked(now)
            previous = self._values.get(cache_key)
            if previous is not None:
                self._discard_locked(
                    cache_key,
                    expired=previous.expires_at <= now,
                )
            self._sequence += 1
            entry = _CacheEntry(
                result=result,
                expires_at=now + ttl_seconds,
                approx_bytes=approx_bytes,
                sequence=self._sequence,
            )
            self._values[cache_key] = entry
            self._approx_bytes += approx_bytes
            insort(
                self._expirations,
                (entry.expires_at, entry.sequence, cache_key),
            )
            self._enforce_limits_locked(now)

    def prune(self, *, max_items: int | None = None) -> int:
        """在固定预算内移除最早到期的结果。

        Args:
            max_items: 本次最多移除的到期条目数；默认使用构造预算。

        Returns:
            实际移除的到期条目数。

        Raises:
            ValueError: 显式预算不是正数。
            RuntimeError: 缓存已经关闭。

        """
        limit = self._prune_batch_size if max_items is None else max_items
        if limit <= 0:
            raise ValueError("Retrieval cache prune max_items 必须为正数。")
        with self._lock:
            self._ensure_open_locked()
            return self._prune_expired_locked(self._clock(), limit)

    def metrics(self) -> RetrievalCacheMetrics:
        """返回不含缓存键、问题、答案或证据正文的指标。

        Args:
            无参数；读取当前缓存计数。

        Returns:
            当前容量与累计命中、淘汰、到期计数。

        """
        with self._lock:
            return RetrievalCacheMetrics(
                entries=len(self._values),
                approx_bytes=self._approx_bytes,
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
                expired=self._expired,
            )

    def close(self) -> None:
        """清空进程内正文并幂等关闭。

        Args:
            无参数；关闭当前缓存。

        Returns:
            无返回值。

        """
        with self._lock:
            self._values.clear()
            self._expirations.clear()
            self._approx_bytes = 0
            self._closed = True

    def _maybe_prune_locked(self, now: float) -> None:
        """按操作次数触发低频、定额的 TTL 清理。"""
        self._operations_since_prune += 1
        if self._operations_since_prune < self._prune_every_operations:
            return
        self._operations_since_prune = 0
        self._prune_expired_locked(now, self._prune_batch_size)

    def _prune_expired_locked(self, now: float, limit: int) -> int:
        """利用有序到期索引移除不超过 limit 个条目。"""
        removed = 0
        while (
            removed < limit
            and self._expirations
            and self._expirations[0][0] <= now
        ):
            _, _, cache_key = self._expirations[0]
            self._discard_locked(cache_key, expired=True)
            removed += 1
        return removed

    def _enforce_limits_locked(self, now: float) -> None:
        """容量承压时优先清到期项，再确定性淘汰 LRU。"""
        while self._over_limit_locked():
            if self._expirations and self._expirations[0][0] <= now:
                _, _, cache_key = self._expirations[0]
                self._discard_locked(cache_key, expired=True)
                continue
            cache_key = next(iter(self._values))
            self._discard_locked(cache_key, evicted=True)

    def _over_limit_locked(self) -> bool:
        return len(self._values) > self._max_entries or (
            self._max_approx_bytes is not None
            and self._approx_bytes > self._max_approx_bytes
        )

    def _discard_locked(
        self,
        cache_key: str,
        *,
        expired: bool = False,
        evicted: bool = False,
    ) -> None:
        """同步删除 LRU 值与唯一到期索引。"""
        entry = self._values.pop(cache_key)
        token = (entry.expires_at, entry.sequence, cache_key)
        index = bisect_left(self._expirations, token)
        if index >= len(self._expirations) or self._expirations[index] != token:
            raise RuntimeError("Retrieval cache 到期索引与条目不一致。")
        self._expirations.pop(index)
        self._approx_bytes -= entry.approx_bytes
        if expired:
            self._expired += 1
        if evicted:
            self._evictions += 1

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise RuntimeError("Retrieval cache 已关闭。")


def _entry_approx_bytes(cache_key: str, result: SearchAnswerResult) -> int:
    """估算 key、回答、证据和 related content 的 UTF-8 载荷。"""
    payload = result.model_dump_json().encode("utf-8")
    return len(cache_key.encode("utf-8")) + len(payload) + _ENTRY_OVERHEAD_BYTES


__all__ = ["InMemoryRetrievalCache", "RetrievalCacheMetrics"]
