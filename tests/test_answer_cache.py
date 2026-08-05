import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rag_app.generation.answer import (
    AnswerClaim,
    AnswerMode,
    AnswerResult,
    AnswerStatus,
    ClaimSupport,
    RefusalCode,
)
from rag_app.state.answer_cache import (
    AnswerCache,
    AnswerCacheKey,
    CacheStoreStatus,
)


def _key(
    *,
    query: str = "验收测试包括哪些内容？",
    manifest: str = "a" * 64,
    serving: str = "sha256:" + "b" * 64,
) -> AnswerCacheKey:
    return AnswerCacheKey.from_inputs(
        resolved_query=query,
        conversation_context_digest="",
        index_manifest_sha256=manifest,
        serving_fingerprint=serving,
        access_mode="source-only",
        answer_revision="sha256:" + "c" * 64,
    )


def _answered() -> AnswerResult:
    support = ClaimSupport(
        evidence_id="E1",
        chunk_id="chunk-1",
        quote="验收测试包括功能、性能和文档验收。",
        locator="规范.docx > 验收 > 段落1",
    )
    claim = AnswerClaim(
        text="验收测试包括功能、性能和文档验收。",
        supports=(support,),
    )
    return AnswerResult(
        status=AnswerStatus.ANSWERED,
        answer=claim.text,
        claims=(claim,),
        refusal_code=None,
        model_calls=1,
        calls=(),
        answer_mode=AnswerMode.ANSWERED,
        user_message=None,
    )


def _not_found() -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.REFUSED,
        answer=None,
        claims=(),
        refusal_code=RefusalCode.EVIDENCE_INSUFFICIENT,
        model_calls=0,
        calls=(),
        answer_mode=AnswerMode.NOT_FOUND,
        user_message=(
            "知识库中暂未找到能够支持该问题的资料。请核对项目名称、编号或时间，"
            "或补充相关文档。"
        ),
    )


def test_exact_answer_cache_binds_manifest_serving_and_protocol(
    tmp_path: Path,
) -> None:
    cache = AnswerCache(tmp_path / "answers.sqlite3")
    cache.initialize()
    now = datetime(2026, 8, 5, tzinfo=UTC)
    key = _key(query="  验收测试包括哪些内容？  ")

    assert cache.store(key, _answered(), now=now) is CacheStoreStatus.STORED
    started = time.perf_counter()
    cached = cache.lookup(
        _key(query="验收测试包括哪些内容？"),
        now=now,
    )
    elapsed = time.perf_counter() - started

    assert cached is not None
    assert cached.answer == _answered().answer
    assert cached.model_calls == 0
    assert cached.calls == ()
    assert elapsed < 0.2
    assert cache.lookup(_key(manifest="d" * 64), now=now) is None
    assert cache.lookup(
        _key(serving="sha256:" + "e" * 64),
        now=now,
    ) is None


def test_not_found_cache_expires_after_ten_minutes(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path / "answers.sqlite3")
    cache.initialize()
    now = datetime(2026, 8, 5, tzinfo=UTC)
    key = _key(query="火星项目负责人是谁？")

    assert cache.store(key, _not_found(), now=now) is CacheStoreStatus.STORED
    assert cache.lookup(key, now=now + timedelta(minutes=9)) is not None
    assert cache.lookup(key, now=now + timedelta(minutes=11)) is None


def test_transient_failure_is_not_cached(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path / "answers.sqlite3")
    cache.initialize()
    result = AnswerResult(
        status=AnswerStatus.REFUSED,
        answer=None,
        claims=(),
        refusal_code=RefusalCode.MODEL_UNAVAILABLE,
        model_calls=1,
        calls=(),
        answer_mode=AnswerMode.INTERNAL_VALIDATION_ERROR,
        user_message="回答服务暂时不可用，请稍后重试并查看 Trace。",
    )

    status = cache.store(
        _key(),
        result,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert status is CacheStoreStatus.SKIPPED


def test_only_answered_and_partial_modes_are_persisted(
    tmp_path: Path,
) -> None:
    cache = AnswerCache(tmp_path / "answers.sqlite3")
    cache.initialize()
    now = datetime(2026, 8, 5, tzinfo=UTC)

    assert cache.store(
        _key(query="部分回答"),
        replace(_answered(), answer_mode=AnswerMode.PARTIAL),
        now=now,
    ) is CacheStoreStatus.STORED
    assert cache.store(
        _key(query="来源冲突"),
        replace(_answered(), answer_mode=AnswerMode.CONFLICT),
        now=now,
    ) is CacheStoreStatus.SKIPPED
    assert cache.store(
        _key(query="抽取回退"),
        replace(_answered(), answer_mode=AnswerMode.EXTRACTIVE_FALLBACK),
        now=now,
    ) is CacheStoreStatus.SKIPPED


def test_singleflight_allows_only_one_concurrent_leader(
    tmp_path: Path,
) -> None:
    cache = AnswerCache(tmp_path / "answers.sqlite3")
    cache.initialize()
    key = _key()
    now = datetime(2026, 8, 5, tzinfo=UTC)
    start = threading.Barrier(4)
    leader_count = 0
    observed: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        nonlocal leader_count
        start.wait()
        with cache.singleflight(key) as flight:
            if flight.is_leader:
                with lock:
                    leader_count += 1
                time.sleep(0.05)
                cache.store(key, _answered(), now=now)
                cache.publish_singleflight(key, _answered())
                observed.append("leader")
            else:
                assert flight.waited is True
                assert flight.result is not None
                assert flight.result.model_calls == 0
                assert cache.lookup(key, now=now) is not None
                observed.append("follower")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert leader_count == 1
    assert sorted(observed) == [
        "follower",
        "follower",
        "follower",
        "leader",
    ]
