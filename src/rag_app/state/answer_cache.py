"""与活动索引和回答协议绑定的 SQLite 精确回答缓存。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

from rag_app.generation.answer import (
    AnswerClaim,
    AnswerMode,
    AnswerResult,
    AnswerStatus,
    ClaimSupport,
    RefusalCode,
)

__all__ = [
    "AnswerCache",
    "AnswerCacheKey",
    "CacheStoreStatus",
    "SingleflightResult",
]

_ANSWER_TTL = timedelta(hours=24)
_NOT_FOUND_TTL = timedelta(minutes=10)


class CacheStoreStatus(StrEnum):
    """缓存写入的稳定结果。"""

    STORED = "stored"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class AnswerCacheKey:
    """精确回答缓存的全部失效维度。"""

    digest: str

    @classmethod
    def from_inputs(  # noqa: PLR0913
        cls,
        *,
        resolved_query: str,
        conversation_context_digest: str,
        index_manifest_sha256: str,
        serving_fingerprint: str,
        access_mode: str,
        answer_revision: str,
    ) -> Self:
        """规范化问题并绑定索引、服务和协议版本。"""
        normalized_query = " ".join(resolved_query.split()).casefold()
        if not normalized_query:
            raise ValueError("resolved_query 不能为空。")
        canonical = json.dumps(
            {
                "access_mode": access_mode,
                "answer_revision": answer_revision,
                "conversation_context_digest": conversation_context_digest,
                "index_manifest_sha256": index_manifest_sha256,
                "resolved_query": normalized_query,
                "serving_fingerprint": serving_fingerprint,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return cls(hashlib.sha256(canonical.encode("utf-8")).hexdigest())


@dataclass(frozen=True, slots=True)
class SingleflightResult:
    """一次同键请求合并的角色和等待状态。"""

    is_leader: bool
    waited: bool
    result: AnswerResult | None = None


@dataclass(slots=True)
class _FlightState:
    """同键并发请求共享的完成信号和瞬时结果。"""

    event: threading.Event
    result: AnswerResult | None = None


class AnswerCache:
    """线程安全的短期精确缓存和进程内 singleflight。"""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._flight_lock = threading.Lock()
        self._flights: dict[str, _FlightState] = {}

    def initialize(self) -> None:
        """创建缓存父目录和幂等表结构。"""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS answer_cache (
                    cache_key TEXT PRIMARY KEY,
                    expires_at TEXT NOT NULL,
                    result_json TEXT NOT NULL
                )
                """
            )

    def lookup(
        self,
        key: AnswerCacheKey,
        *,
        now: datetime | None = None,
    ) -> AnswerResult | None:
        """返回未过期精确命中，并把外部调用计数清零。"""
        current = _utc(now)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT expires_at, result_json
                FROM answer_cache
                WHERE cache_key = ?
                """,
                (key.digest,),
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(str(row[0])) <= current:
                connection.execute(
                    "DELETE FROM answer_cache WHERE cache_key = ?",
                    (key.digest,),
                )
                return None
        return _decode_result(str(row[1]))

    def store(
        self,
        key: AnswerCacheKey,
        result: AnswerResult,
        *,
        now: datetime | None = None,
    ) -> CacheStoreStatus:
        """只缓存已发布回答和明确未找到，跳过临时失败。"""
        ttl = _cache_ttl(result)
        if ttl is None:
            return CacheStoreStatus.SKIPPED
        current = _utc(now)
        expires_at = current + ttl
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO answer_cache(cache_key, expires_at, result_json)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    expires_at = excluded.expires_at,
                    result_json = excluded.result_json
                """,
                (
                    key.digest,
                    expires_at.isoformat(),
                    _encode_result(result),
                ),
            )
        return CacheStoreStatus.STORED

    def singleflight(
        self,
        key: AnswerCacheKey,
    ) -> AbstractContextManager[SingleflightResult]:
        """合并当前进程中正在执行的相同缓存键请求。"""
        return _SingleflightContext(self, key.digest)

    def publish_singleflight(
        self,
        key: AnswerCacheKey,
        result: AnswerResult,
    ) -> None:
        """向同键等待者发布本次结果，不改变持久化缓存策略。

        Args:
            key: 当前请求的完整精确缓存键。
            result: 领导请求已经完成门禁的最终结果。

        Returns:
            无返回值；没有活动请求时安全忽略。

        """
        shared_result = _decode_result(_encode_result(result))
        with self._flight_lock:
            state = self._flights.get(key.digest)
            if state is not None:
                state.result = shared_result

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._database_path, timeout=30)


class _SingleflightContext(AbstractContextManager[SingleflightResult]):
    def __init__(self, cache: AnswerCache, digest: str) -> None:
        self._cache = cache
        self._digest = digest
        self._state: _FlightState | None = None
        self._leader = False

    def __enter__(self) -> SingleflightResult:
        with self._cache._flight_lock:
            state = self._cache._flights.get(self._digest)
            if state is None:
                state = _FlightState(event=threading.Event())
                self._cache._flights[self._digest] = state
                self._leader = True
            self._state = state
        if not self._leader:
            state.event.wait()
        return SingleflightResult(
            is_leader=self._leader,
            waited=not self._leader,
            result=state.result,
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if self._leader and self._state is not None:
            with self._cache._flight_lock:
                current = self._cache._flights.get(self._digest)
                if current is self._state:
                    self._cache._flights.pop(self._digest, None)
                self._state.event.set()
        return None


def _cache_ttl(result: AnswerResult) -> timedelta | None:
    if (
        result.status is AnswerStatus.ANSWERED
        and result.answer_mode in {AnswerMode.ANSWERED, AnswerMode.PARTIAL}
    ):
        return _ANSWER_TTL
    if result.answer_mode is AnswerMode.NOT_FOUND:
        return _NOT_FOUND_TTL
    return None


def _encode_result(result: AnswerResult) -> str:
    return json.dumps(
        {
            "answer": result.answer,
            "answer_mode": result.answer_mode.value,
            "claims": [
                {
                    "text": claim.text,
                    "supports": [
                        {
                            "chunk_id": support.chunk_id,
                            "evidence_id": support.evidence_id,
                            "locator": support.locator,
                            "quote": support.quote,
                        }
                        for support in claim.supports
                    ],
                }
                for claim in result.claims
            ],
            "refusal_code": (
                None
                if result.refusal_code is None
                else result.refusal_code.value
            ),
            "status": result.status.value,
            "user_message": result.user_message,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_result(serialized: str) -> AnswerResult:
    payload = json.loads(serialized)
    claims = tuple(
        AnswerClaim(
            text=str(claim["text"]),
            supports=tuple(
                ClaimSupport(
                    evidence_id=str(support["evidence_id"]),
                    chunk_id=str(support["chunk_id"]),
                    quote=str(support["quote"]),
                    locator=str(support["locator"]),
                )
                for support in claim["supports"]
            ),
        )
        for claim in payload["claims"]
    )
    raw_refusal = payload["refusal_code"]
    return AnswerResult(
        status=AnswerStatus(payload["status"]),
        answer=payload["answer"],
        claims=claims,
        refusal_code=(
            None if raw_refusal is None else RefusalCode(raw_refusal)
        ),
        model_calls=0,
        calls=(),
        answer_mode=AnswerMode(payload["answer_mode"]),
        user_message=payload["user_message"],
        trace={"cache_status": "hit"},
    )


def _utc(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if current.tzinfo is None:
        raise ValueError("缓存时间必须包含时区。")
    return current.astimezone(UTC)
