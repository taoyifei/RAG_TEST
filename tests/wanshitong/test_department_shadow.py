"""阶段 04 部门 Shadow Routing 的纯函数场景门禁。"""

from __future__ import annotations

from datetime import UTC, datetime

from rag_app.core.identifiers import canonical_sha256
from rag_app.wanshitong.department_profiles import (
    DepartmentEmbeddingIdentity,
    DepartmentProfileSet,
    DepartmentProfileSourceDocument,
    DepartmentProfileSourceSnapshot,
    build_department_profiles,
)
from rag_app.wanshitong.department_shadow import (
    DepartmentRecommendedScope,
    DepartmentRouteConfidence,
    DepartmentRouteStatus,
    ReusableRootVector,
    suggest_department,
)

_PROJECT_ID = "prj_" + "1" * 32
_KNOWLEDGE_BASE_ID = "kb_" + "2" * 32
_INDEX_REVISION_ID = "irev_" + "3" * 32
_BUILT_AT = datetime(2026, 9, 21, tzinfo=UTC)


def _profiles(*, vectors: bool = False) -> DepartmentProfileSet:
    documents = (
        DepartmentProfileSourceDocument(
            document_id="doc_" + "4" * 32,
            document_version_id="dver_" + "4" * 32,
            department_key="research",
            department_name="科研部",
            aliases=("科技管理部",),
            category_path=("科研项目",),
            document_title="科研项目管理办法",
            topic_keys=("项目申报",),
            metadata_revision="wanshitong-document-metadata-v1",
        ),
        DepartmentProfileSourceDocument(
            document_id="doc_" + "5" * 32,
            document_version_id="dver_" + "5" * 32,
            department_key="finance",
            department_name="财务部",
            category_path=("财务制度",),
            document_title="差旅报销规则",
            topic_keys=("发票报销",),
            metadata_revision="wanshitong-document-metadata-v1",
        ),
    )
    snapshot = DepartmentProfileSourceSnapshot(
        project_id=_PROJECT_ID,
        knowledge_base_id=_KNOWLEDGE_BASE_ID,
        scope_id=canonical_sha256(
            {
                "project_id": _PROJECT_ID,
                "knowledge_base_id": _KNOWLEDGE_BASE_ID,
            }
        ),
        index_revision_id=_INDEX_REVISION_ID,
        metadata_revision=canonical_sha256(
            [document.model_dump(mode="json") for document in documents]
        ),
        documents=documents,
    )
    identity = DepartmentEmbeddingIdentity(
        slot_id="primary",
        provider_id="embedding",
        model="model",
        vector_name="dense_primary",
        dimension=2,
        normalization="l2",
        adapter_revision="1",
    )
    return build_department_profiles(
        snapshot,
        embedding_identity=identity if vectors else None,
        vectors={"research": (1.0, 0.0), "finance": (-1.0, 0.0)}
        if vectors
        else None,
        built_at=_BUILT_AT,
    )


def test_explicit_department_and_alias_are_high_top1() -> None:
    profiles = _profiles()

    direct = suggest_department(
        "科研部负责什么", "CURRENT:HIGH", profiles, None
    )
    alias = suggest_department(
        "科技管理部有哪些制度", "CURRENT:HIGH", profiles, None
    )

    assert direct.top1_department_key == "research"
    assert alias.top1_department_key == "research"
    assert direct.confidence is DepartmentRouteConfidence.HIGH
    assert direct.recommended_scope is DepartmentRecommendedScope.TOP1


def test_title_relation_uses_lexical_profile_without_vector() -> None:
    suggestion = suggest_department(
        "科研 项目 管理", "CURRENT:HIGH", _profiles(), None
    )

    assert suggestion.top1_department_key == "research"
    assert suggestion.recommended_scope is DepartmentRecommendedScope.TOP1
    assert suggestion.embedding_reused is False
    assert "ROOT_VECTOR_UNAVAILABLE" in suggestion.reason_codes


def test_cross_department_and_multiple_names_do_not_force_top1() -> None:
    profiles = _profiles()

    lexical = suggest_department(
        "科研管理 财务报销", "CURRENT:HIGH", profiles, None
    )
    explicit = suggest_department(
        "科研部和财务部各自负责什么", "CURRENT:HIGH", profiles, None
    )

    assert lexical.recommended_scope is not DepartmentRecommendedScope.TOP1
    assert explicit.recommended_scope is not DepartmentRecommendedScope.TOP1
    assert "EXPLICIT_DEPARTMENT_AMBIGUOUS" in explicit.reason_codes


def test_no_department_terms_and_low_context_fall_back_global() -> None:
    profiles = _profiles()

    unrelated = suggest_department(
        "今天天气如何", "CURRENT:HIGH", profiles, None
    )
    follow_up = suggest_department("那这个呢", "CLARIFY:LOW", profiles, None)

    assert unrelated.recommended_scope is DepartmentRecommendedScope.GLOBAL
    assert unrelated.status is DepartmentRouteStatus.FALLBACK
    assert follow_up.recommended_scope is DepartmentRecommendedScope.GLOBAL
    assert "CONTEXT_LOW_CONFIDENCE" in follow_up.reason_codes


def test_explicit_document_scope_is_advisory_without_filter() -> None:
    suggestion = suggest_department(
        "请概括这份文件",
        "CURRENT:HIGH",
        _profiles(),
        None,
        ("doc_" + "5" * 32,),
    )

    assert suggestion.top1_department_key == "finance"
    assert suggestion.recommended_scope is DepartmentRecommendedScope.TOP1
    assert suggestion.department_filter_applied is False
    assert suggestion.extra_provider_calls == 0
    assert suggestion.reason_codes == ("EXPLICIT_SOURCE_DEPARTMENT",)


def test_root_vector_reuse_requires_matching_identity() -> None:
    profiles = _profiles(vectors=True)
    identity = profiles.embedding_identity
    assert identity is not None

    reused = suggest_department(
        "完全无词面交集",
        "CURRENT:HIGH",
        profiles,
        ReusableRootVector(identity=identity, values=(1.0, 0.0)),
    )
    mismatched = suggest_department(
        "完全无词面交集",
        "CURRENT:HIGH",
        profiles,
        ReusableRootVector(
            identity=identity.model_copy(update={"model": "other-model"}),
            values=(1.0, 0.0),
        ),
    )

    assert reused.top1_department_key == "research"
    assert reused.embedding_reused is True
    assert "ROOT_VECTOR_REUSED" in reused.reason_codes
    assert mismatched.embedding_reused is False
    assert "VECTOR_IDENTITY_MISMATCH" in mismatched.reason_codes
