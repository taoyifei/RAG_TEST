"""F06 审核目录、逐请求 alias 去重及公共榜降级验收。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from httpx import Response

from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from rag_app.wanshitong.public_api import PUBLIC_POPULAR_QUESTIONS_PATH
from tests.wanshitong.support import PublicHarness
from tests.wanshitong.test_question_analytics import (
    _add_request,
    _complete_run,
    _scope,
    _service,
)


def _install_current_source(harness: PublicHarness) -> dict[str, str]:
    """建立一条真实受当前索引引用的测试文档版本。"""
    scope = _scope(harness)
    document_id = "doc_" + uuid.uuid4().hex
    version_id = "dver_" + uuid.uuid4().hex
    revision_id = "irev_" + uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    with harness.product.runtime.connections.transaction(write=True) as conn:
        conn.execute(
            "INSERT INTO documents (document_id, project_id, "
            "knowledge_base_id, display_name, status, created_at, updated_at) "
            "VALUES (?, ?, ?, '审核依据', 'active', ?, ?)",
            (document_id, scope.project_id, scope.knowledge_base_id, now, now),
        )
        conn.execute(
            "INSERT INTO document_versions (document_version_id, document_id, "
            "content_sha256, source_artifact_id, size_bytes, media_type, "
            "created_at) VALUES (?, ?, ?, 'blob_test', 1, 'text/plain', ?)",
            (version_id, document_id, "a" * 64, now),
        )
        conn.execute(
            "UPDATE documents SET current_version_id=? WHERE document_id=?",
            (version_id, document_id),
        )
        conn.execute(
            "INSERT INTO index_revisions (index_revision_id, project_id, "
            "knowledge_base_id, state, index_fingerprint, "
            "serving_compatibility_version, parser_identity_json, "
            "parsing_policy_json, chunker_identity_json, chunking_policy_json, "
            "embedding_topology_json, lexical_schema_json, "
            "vector_schema_json, chunk_payload_schema_json, "
            "physical_vector_namespace, expected_document_count, "
            "expected_chunk_count, created_at) VALUES "
            "(?, ?, ?, 'active', ?, 'v1', '{}', '{}', '{}', '{}', "
            "'{}', '{}', '{}', '{}', ?, 1, 0, ?)",
            (
                revision_id,
                scope.project_id,
                scope.knowledge_base_id,
                "sha256:" + "b" * 64,
                "test_" + revision_id,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO revision_documents (revision_id, document_id, "
            "document_version_id, parser_id, parser_version, "
            "parsing_policy_fingerprint, ir_schema_version, document_ir_json, "
            "parse_report_json, chunking_report_json, "
            "part_catalog_identity, chunk_count) VALUES "
            "(?, ?, ?, 'test', 'v1', 'test', 'v1', '{}', '{}', '{}', "
            "'test', 0)",
            (revision_id, document_id, version_id),
        )
        conn.execute(
            "UPDATE knowledge_bases SET active_revision_id=? "
            "WHERE knowledge_base_id=?",
            (revision_id, scope.knowledge_base_id),
        )
    return {"document_id": document_id, "version_id": version_id}


def _candidate(harness: PublicHarness, group_key: str, question: str) -> dict:
    response = harness.client.post(
        ADMIN_BASE_PATH + "/recommendations",
        headers=harness.product.write_headers,
        json={
            "source_group_key": group_key,
            "question_text": question,
            "topic_key": "办理",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _update(  # noqa: PLR0913
    harness: PublicHarness,
    item: dict,
    *,
    state: str,
    aliases: list[str],
    sources: list[dict[str, str]],
    question: str = "办理材料如何提交？",
    disabled_reason: str | None = None,
) -> Response:
    return harness.client.patch(
        ADMIN_BASE_PATH + "/recommendations/" + item["recommendation_id"],
        headers=harness.product.write_headers,
        json={
            "expected_version": item["version"],
            "question_text": question,
            "question_style": "SHORT",
            "topic_key": "办理",
            "state": state,
            "alias_keys": aliases,
            "validated_sources": sources,
            "review_confirmed": state == "APPROVED",
            "disabled_reason": disabled_reason,
        },
    )


def test_alias_recounts_distinct_and_disable_is_immediate(
    public_harness: PublicHarness,
) -> None:
    """两种问法有共享 owner，合并与拆分均从原始请求重算。"""
    state = public_harness.app.state
    state.wanshitong_question_recommendations.deployment_id = "candidate_8289"
    source = _install_current_source(public_harness)
    for question, owner in (
        ("材料怎么交？", "rdms:1"),
        ("材料怎么交？", "rdms:1"),
        ("材料怎么交？", "rdms:2"),
        ("材料如何提交？", "rdms:1"),
        ("材料如何提交？", "rdms:3"),
    ):
        _add_request(public_harness, question, owner_id=owner)
    _add_request(
        public_harness,
        "材料怎么交？",
        owner_id="rdms:production",
        deployment_id="production_18288",
    )
    scope = _scope(public_harness)
    service = _service(public_harness)
    assert _complete_run(service, scope)["state"] == "COMPLETE"
    first_board = service.board(
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        board="frequent",
    )
    assert len(first_board["items"]) == 2
    keys = [item["group_key"] for item in first_board["items"]]
    draft = _candidate(public_harness, keys[0], "私人原问，请改写")
    assert draft["state"] == "DRAFT"
    assert (
        public_harness.client.get(PUBLIC_POPULAR_QUESTIONS_PATH).json()["mode"]
        == "EMPTY"
    )

    approved_response = _update(
        public_harness,
        draft,
        state="APPROVED",
        aliases=keys,
        sources=[source],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    assert (
        public_harness.client.get(PUBLIC_POPULAR_QUESTIONS_PATH).json()["mode"]
        == "EMPTY"
    )
    assert _complete_run(service, scope)["state"] == "COMPLETE"
    combined = service.board(
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        board="frequent",
    )["items"][0]
    assert combined["group_kind"] == "APPROVED_ALIAS"
    assert combined["request_count"] == 5
    assert combined["manual_distinct_users"] == 3
    popular = public_harness.client.get(PUBLIC_POPULAR_QUESTIONS_PATH)
    assert popular.status_code == 200
    assert popular.json()["mode"] == "POPULAR"
    assert popular.json()["items"] == [
        {
            "id": approved["recommendation_id"],
            "question": "办理材料如何提交？",
            "topic_key": "办理",
        }
    ]
    assert "私人原问" not in popular.text
    assert "rdms:" not in popular.text
    assert (
        _update(
            public_harness,
            approved,
            state="APPROVED",
            aliases=keys,
            sources=[source],
            question="不同业务的问题如何办理？",
        ).status_code
        == 409
    )
    assert (
        _update(
            public_harness,
            approved,
            state="APPROVED",
            aliases=[*keys, "f" * 64],
            sources=[source],
        ).status_code
        == 409
    )
    revised_draft = _update(
        public_harness,
        approved,
        state="DRAFT",
        aliases=keys,
        sources=[source],
        question="办理资料的提交方式是什么？",
    )
    assert revised_draft.status_code == 200, revised_draft.text
    revised_approved = _update(
        public_harness,
        revised_draft.json(),
        state="APPROVED",
        aliases=keys,
        sources=[source],
        question="办理资料的提交方式是什么？",
    )
    assert revised_approved.status_code == 200, revised_approved.text
    assert (
        public_harness.client.get(PUBLIC_POPULAR_QUESTIONS_PATH).json()["mode"]
        == "EMPTY"
    )
    assert _complete_run(service, scope)["state"] == "COMPLETE"
    approved = revised_approved.json()

    split = _update(
        public_harness,
        approved,
        state="APPROVED",
        aliases=[keys[0]],
        sources=[source],
        question="办理资料的提交方式是什么？",
    )
    assert split.status_code == 200, split.text
    assert (
        public_harness.client.get(PUBLIC_POPULAR_QUESTIONS_PATH).json()["mode"]
        == "EMPTY"
    )
    assert _complete_run(service, scope)["state"] == "COMPLETE"
    disabled = _update(
        public_harness,
        split.json(),
        state="DISABLED",
        aliases=[keys[0]],
        sources=[source],
        question="办理资料的提交方式是什么？",
        disabled_reason="题面暂不适合公开",
    )
    assert disabled.status_code == 200, disabled.text
    assert (
        public_harness.client.get(PUBLIC_POPULAR_QUESTIONS_PATH).json()["mode"]
        == "EMPTY"
    )


def test_admin_auth_alias_conflict_and_source_replacement(
    public_harness: PublicHarness,
) -> None:
    """普通用户不能管理；冲突不覆盖；资料版本变更后下次 GET 下架。"""
    state = public_harness.app.state
    state.wanshitong_question_recommendations.deployment_id = "candidate_8289"
    source = _install_current_source(public_harness)
    for owner in ("rdms:1", "rdms:2", "rdms:3", "rdms:1", "rdms:2"):
        _add_request(public_harness, "如何办理？", owner_id=owner)
    scope = _scope(public_harness)
    service = _service(public_harness)
    _complete_run(service, scope)
    key = service.board(
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        board="frequent",
    )["items"][0]["group_key"]
    with TestClient(public_harness.app) as unauthenticated:
        assert (
            unauthenticated.get(
                ADMIN_BASE_PATH + "/recommendations"
            ).status_code
            == 401
        )
        assert (
            unauthenticated.get(PUBLIC_POPULAR_QUESTIONS_PATH).status_code
            == 401
        )
    draft = _candidate(public_harness, key, "原始私人题")
    approved = _update(
        public_harness,
        draft,
        state="APPROVED",
        aliases=[key],
        sources=[source],
    ).json()
    duplicate = _update(
        public_harness,
        draft,
        state="APPROVED",
        aliases=[key],
        sources=[source],
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["version"] == approved["version"]
    other = _candidate(public_harness, key, "另一个候选")
    assert (
        _update(
            public_harness,
            other,
            state="APPROVED",
            aliases=[key],
            sources=[source],
        ).status_code
        == 409
    )
    assert _complete_run(service, scope)["state"] == "COMPLETE"
    assert (
        public_harness.client.get(PUBLIC_POPULAR_QUESTIONS_PATH).json()["mode"]
        == "POPULAR"
    )
    with public_harness.product.runtime.connections.transaction(
        write=True
    ) as connection:
        connection.execute(
            "UPDATE documents SET status='deleted', deleted_at=? "
            "WHERE document_id=?",
            (datetime.now(UTC).isoformat(), source["document_id"]),
        )
    assert (
        public_harness.client.get(PUBLIC_POPULAR_QUESTIONS_PATH).json()["mode"]
        == "EMPTY"
    )
    listing = public_harness.client.get(ADMIN_BASE_PATH + "/recommendations")
    assert listing.status_code == 200
    assert (
        next(
            item
            for item in listing.json()["items"]
            if item["recommendation_id"] == approved["recommendation_id"]
        )["state"]
        == "NEEDS_REVIEW"
    )
