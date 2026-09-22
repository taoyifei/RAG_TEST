"""阶段 04 部门 Profile 离线向量构建门禁。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import EmbeddingRequest, EmbeddingResult
from rag_app.wanshitong.department_profiles import (
    DepartmentProfileSourceDocument,
    DepartmentProfileSourceSnapshot,
)
from rag_app.wanshitong.internal_model_settings import InternalModelSettings
from scripts import build_wanshitong_department_profiles as builder


class _EmbeddingAdapter:
    """返回固定向量的既有 Adapter 测试替身。"""

    request_texts: tuple[str, ...] = ()
    closed = False

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        texts = request.texts
        type(self).request_texts = texts
        return EmbeddingResult(
            slot_id="primary",
            role=request.role,
            vectors=tuple((1.0, 0.0) for _ in texts),
            observed_dimension=2,
            request_policy_identity="document-policy",
        )

    def close(self) -> None:
        type(self).closed = True


def test_existing_provider_mode_binds_vectors_to_primary_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = DepartmentProfileSourceDocument(
        document_id="doc_" + "1" * 32,
        document_version_id="dver_" + "2" * 32,
        department_key="research",
        department_name="科研部",
        document_title="科研项目管理办法",
        metadata_revision="metadata-v1",
    )
    snapshot = DepartmentProfileSourceSnapshot(
        project_id="prj_" + "3" * 32,
        knowledge_base_id="kb_" + "4" * 32,
        scope_id=canonical_sha256({"scope": "department-profile-test"}),
        index_revision_id="irev_" + "5" * 32,
        metadata_revision=canonical_sha256([document.model_dump(mode="json")]),
        documents=(document,),
    )
    settings = InternalModelSettings(
        embedding_base_url="http://embedding.internal/v1",
        reranker_base_url="http://reranker.internal",
        llm_base_url="http://llm.internal/v1",
        embedding_dimension=2,
    )
    monkeypatch.setattr(
        builder.InternalModelSettings,
        "from_environment",
        lambda: settings,
    )
    monkeypatch.setattr(
        builder,
        "OpenAICompatibleEmbeddingAdapter",
        _EmbeddingAdapter,
    )

    profiles, provider_call_count = builder._build_vectors(snapshot)

    assert provider_call_count == 0
    assert _EmbeddingAdapter.closed is True
    assert len(_EmbeddingAdapter.request_texts) == 1
    assert profiles.built_at <= datetime.now(UTC)
    assert profiles.embedding_identity is not None
    assert profiles.embedding_identity.model == "Qwen3-Embedding-0.6B"
    assert profiles.embedding_identity.dimension == 2
    assert profiles.embedding_identity.provider_id == (
        "openai-compatible-embedding"
    )
    assert profiles.profiles[0].vector == (1.0, 0.0)
