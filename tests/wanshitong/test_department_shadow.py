"""阶段 04 部门 Shadow Routing 的纯函数场景门禁。"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import EmbeddingSlotIdentity, EmbeddingSlotRole
from rag_app.core.ports import DepartmentShadowRequest
from rag_app.wanshitong.department_profiles import (
    DepartmentEmbeddingIdentity,
    DepartmentProfileLoadError,
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
    WanshitongDepartmentShadowObserver,
    suggest_department,
)

_PROJECT_ID = "prj_" + "1" * 32
_KNOWLEDGE_BASE_ID = "kb_" + "2" * 32
_INDEX_REVISION_ID = "irev_" + "3" * 32
_BUILT_AT = datetime(2026, 9, 21, tzinfo=UTC)


def _source_snapshot() -> DepartmentProfileSourceSnapshot:
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
    return DepartmentProfileSourceSnapshot(
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


def _profiles(*, vectors: bool = False) -> DepartmentProfileSet:
    snapshot = _source_snapshot()
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


class _StaticSource:
    def __init__(self, snapshot: DepartmentProfileSourceSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(
        self, project_id: str, knowledge_base_id: str
    ) -> DepartmentProfileSourceSnapshot:
        assert project_id == _PROJECT_ID
        assert knowledge_base_id == _KNOWLEDGE_BASE_ID
        return self._snapshot


class _StaticStore:
    def __init__(
        self,
        profiles: DepartmentProfileSet | None,
        error: Exception | None = None,
    ) -> None:
        self._profiles = profiles
        self._error = error

    def load(
        self,
        scope_id: str,
        index_revision_id: str,
        metadata_revision: str,
    ) -> DepartmentProfileSet | None:
        del scope_id, index_revision_id, metadata_revision
        if self._error is not None:
            raise self._error
        return self._profiles


def _request(
    *,
    root_vector: tuple[float, ...] | None = None,
    embedding_slot: EmbeddingSlotIdentity | None = None,
) -> DepartmentShadowRequest:
    return DepartmentShadowRequest(
        project_id=_PROJECT_ID,
        knowledge_base_id=_KNOWLEDGE_BASE_ID,
        index_revision_id=_INDEX_REVISION_ID,
        resolved_root_query="科研部负责什么",
        context_mode="ORIGINAL:HIGH",
        scope_digest=canonical_sha256({"scope": "open"}),
        actual_scope_kind="OPEN",
        explicit_source_document_ids=(),
        final_cited_document_ids=("doc_" + "4" * 32,),
        embedding_slot=embedding_slot,
        root_vector=root_vector,
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


def test_observer_emits_safe_fields_and_posthoc_cited_department() -> None:
    profiles = _profiles(vectors=True)
    identity = profiles.embedding_identity
    assert identity is not None
    observer = WanshitongDepartmentShadowObserver(
        _StaticSource(_source_snapshot()), _StaticStore(profiles)
    )
    slot = EmbeddingSlotIdentity(
        slot_id=identity.slot_id,
        role=EmbeddingSlotRole.PRIMARY,
        provider_id=identity.provider_id,
        model=identity.model,
        vector_name=identity.vector_name,
        dimension=identity.dimension,
        normalization=identity.normalization,
        adapter_revision=identity.adapter_revision,
    )

    observation = observer.observe(
        _request(root_vector=(1.0, 0.0), embedding_slot=slot)
    )
    attributes = observation.trace_attributes()

    assert observation.status == "COMPUTED"
    assert observation.top1_department_key == "research"
    assert observation.final_cited_department_keys == ("research",)
    assert observation.department_filter_applied is False
    assert observation.embedding_reused is False  # 显式部门无需消费向量。
    assert observation.extra_provider_calls == 0
    assert observation.resolved_root_query_sha256 == sha256(
        "科研部负责什么".encode()
    ).hexdigest()
    assert "科研部负责什么" not in repr(attributes)
    assert "root_vector" not in attributes


def test_observer_profile_failures_fall_back_without_throwing() -> None:
    source = _StaticSource(_source_snapshot())
    missing = WanshitongDepartmentShadowObserver(
        source, _StaticStore(None)
    ).observe(_request())
    corrupt = WanshitongDepartmentShadowObserver(
        source,
        _StaticStore(
            None, DepartmentProfileLoadError("synthetic corrupt profile")
        ),
    ).observe(_request())
    mismatched = WanshitongDepartmentShadowObserver(
        _StaticSource(
            _source_snapshot().model_copy(
                update={"index_revision_id": "irev_" + "9" * 32}
            )
        ),
        _StaticStore(_profiles()),
    ).observe(_request())

    assert missing.status == "FALLBACK"
    assert missing.recommended_scope == "GLOBAL"
    assert missing.reason_codes == ("PROFILE_UNAVAILABLE",)
    assert corrupt.reason_codes == ("PROFILE_CORRUPT",)
    assert mismatched.reason_codes == ("PROFILE_VERSION_MISMATCH",)
