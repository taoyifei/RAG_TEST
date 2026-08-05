import hashlib
import json
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

import rag_app.cli as cli_module
import rag_app.runtime as runtime_module
import rag_app.worker_runtime as worker_runtime_module
from rag_app.corpus_policy import CorpusPolicy
from rag_app.freeze_evidence import (
    FreezeCandidate,
    FreezeCandidateConfig,
    FreezeDecision,
    FreezeEvidence,
    FreezeModelRevisions,
    FreezeThresholds,
    ModelContractReportDigest,
)
from rag_app.index.build import DocxBuildConfig
from rag_app.model_contracts import actual_prompt_revision
from rag_app.runtime import build_runtime, load_pipeline
from rag_app.settings import (
    AccessMode,
    ConfigurationState,
    RetrievalSettings,
    RuntimeSettings,
)
from rag_app.worker_runtime import build_worker_runtime


@pytest.fixture(autouse=True)
def _installed_source_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "SOURCE_REVISION", "1" * 40)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _prompt_revision() -> str:
    return actual_prompt_revision()


def test_checked_in_pipeline_matches_actual_prompt_revision() -> None:
    pipeline = load_pipeline(Path("deployment/config/pipeline.json"))

    assert pipeline.prompt_revision == actual_prompt_revision()


def test_abstention_review_changes_serving_but_not_index_fingerprint() -> None:
    pipeline = load_pipeline(Path("deployment/config/pipeline.json"))
    retrieval = RetrievalSettings.load(
        Path("deployment/config/retrieval.json")
    )

    assert pipeline.prompt_revision != (
        "sha256:1d38a149ab6db8d533dabf9e3e5d343b"
        "aa335d2d8bd8b94fdeffafd35f15431e"
    )
    assert pipeline.index_fingerprint() == (
        "sha256:dd16e57d6b39e95af18ea5317d66682"
        "c71f4044e927a09bc6cc0599a8f7f192a"
    )
    assert retrieval.serving_fingerprint(pipeline) != (
        "sha256:a6ba0cd4790839fd8bc115c6d74e1723"
        "e5e8129e5c3408d74371c869e7971631"
    )


def _write_configuration(tmp_path: Path) -> dict[str, Path]:
    llm_tokenizer = tmp_path / "llm-tokenizer.json"
    embedding_tokenizer = tmp_path / "embedding-tokenizer.json"
    unknown_symbol = "[UNK]"
    tokenizer = Tokenizer(
        WordLevel(
            vocab={unknown_symbol: 0, "固定": 1, "文档": 2},
            unk_token=unknown_symbol,
        )
    )
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(llm_tokenizer))
    tokenizer.save(str(embedding_tokenizer))
    corpus_policy = tmp_path / "corpus-policy.json"
    corpus_policy.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "defaults": {
                    "document_status": "active",
                    "authority_level": "official",
                    "effective_from": None,
                    "effective_to": None,
                },
                "overrides": [],
            }
        ),
        encoding="utf-8",
    )
    pipeline_path = tmp_path / "pipeline.json"
    pipeline_path.write_text(
        json.dumps(
            {
                "schema_version": "2",
                "parser_revision": "docx-parser-v3",
                "ocr_model": "ocr",
                "ocr_revision": "ocr-v1",
                "ocr_minimum_confidence": 0.8,
                "chunker_revision": "structural-v1",
                "chunker_parameters": [
                    ["target_tokens", "384"],
                    ["hard_max_tokens", "512"],
                    ["overlap_tokens", "64"],
                ],
                "embedding_model": "embedding-model",
                "embedding_revision": "embedding-v1",
                "embedding_dimension": 4,
                "embedding_tokenizer_sha256": _sha256(
                    embedding_tokenizer.read_bytes()
                ),
                "document_embedding_instruction": "",
                "sparse_model": "qdrant/bm25",
                "sparse_revision": (
                    "sha256:1d55f95e952af834de5d4bdf2e321438eca4bee9f05dd"
                    "309f2029236703e8b12"
                ),
                "sparse_tokenizer": "multilingual",
                "sparse_language": "none",
                "index_revision": "qdrant-v1.18.3",
                "corpus_policy_sha256": CorpusPolicy.load(
                    corpus_policy
                ).semantic_sha256(),
                "reranker_model": "reranker-model",
                "reranker_revision": "reranker-v1",
                "llm_model": "llm-model",
                "llm_revisions": [
                    ["llm-1", "llm-v1"],
                    ["llm-2", "llm-v1"],
                    ["llm-3", "llm-v1"],
                    ["llm-4", "llm-v1"],
                ],
                "prompt_revision": _prompt_revision(),
                "llm_tokenizer_sha256": _sha256(llm_tokenizer.read_bytes()),
            }
        ),
        encoding="utf-8",
    )
    retrieval_path = tmp_path / "retrieval.json"
    retrieval_payload = {
        "status": "provisional",
        "freeze_decision_sha256": None,
        "dense_limit": 40,
        "bm25_limit": 40,
        "rrf_rank_constant": 60,
        "candidate_limit": 24,
        "final_limit": 6,
        "max_final_limit": 8,
        "query_instruction": "检索相关规范",
        "max_history_turns": 3,
        "history_token_budget": 512,
        "max_question_tokens": 512,
        "rewrite_output_tokens": 128,
        "max_evidence_tokens": 4096,
        "low_ocr_threshold": 0.8,
        "answer_output_tokens": 1024,
        "repair_output_tokens": 1024,
        "conversation_ttl_seconds": 1800,
        "bm25_tokenizer": "multilingual",
        "bm25_language": "none",
        "allowed_statuses": ["active"],
        "allowed_authority_levels": ["official"],
        "soft_route_min_confidence": 0.75,
        "soft_routes": [],
    }
    pipeline = load_pipeline(pipeline_path)
    provisional = RetrievalSettings.model_validate(retrieval_payload)
    reports = tuple(
        ModelContractReportDigest(
            name=name,
            sha256="sha256:" + str(index) * 64,
        )
        for index, name in enumerate(
            (
                "model-contract-embedding.json",
                "model-contract-reranker.json",
                "model-contract-llm-1.json",
                "model-contract-llm-2.json",
                "model-contract-llm-3.json",
                "model-contract-llm-4.json",
            ),
            start=1,
        )
    )
    decision = FreezeDecision(
        attempt_id="a" * 32,
        index_fingerprint=pipeline.index_fingerprint(),
        serving_fingerprint=provisional.serving_fingerprint(pipeline),
        model_revisions=FreezeModelRevisions(
            calibration_source_revision="2" * 40,
            embedding_revision=pipeline.embedding_revision,
            reranker_revision=pipeline.reranker_revision,
            llm_revisions=pipeline.llm_revisions,
        ),
        selected_candidate=FreezeCandidate(
            label="section-pack-v2-384-512-64",
            strategy="section_pack_v2",
            chunker_revision=pipeline.chunker_revision,
            config=FreezeCandidateConfig(
                target_tokens=384,
                hard_max_tokens=512,
                overlap_tokens=64,
            ),
        ),
        thresholds=FreezeThresholds(
            recall_at_20=0.96,
            recall_at_20_passed=True,
            rerank_recall_at_5=0.91,
            rerank_recall_at_5_passed=True,
        ),
        evidence=FreezeEvidence(
            pipeline_file_sha256="sha256:" + "a" * 64,
            retrieval_file_sha256="sha256:" + "b" * 64,
            calibration_corpus_manifest_sha256=(
                "sha256:" + "c" * 64
            ),
            final_corpus_manifest_sha256="sha256:" + "d" * 64,
            final_evaluation_manifest_sha256="sha256:" + "e" * 64,
            final_corpus_id="synthetic-v2",
            final_corpus_digest="f" * 64,
            tuning_digest="sha256:" + "a" * 64,
            calibration_structural_report_sha256=(
                "sha256:" + "b" * 64
            ),
            calibration_retrieval_report_sha256=(
                "sha256:" + "c" * 64
            ),
            model_contract_reports=reports,
            model_contract_summary_sha256="sha256:" + "d" * 64,
        ),
    )
    retrieval_path.write_text(
        json.dumps(
            {
                **retrieval_payload,
                "status": ConfigurationState.FROZEN.value,
                "freeze_decision_sha256": decision.sha256(),
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "FREEZE_DECISION.json").write_text(
        json.dumps(decision.model_dump(mode="json")),
        encoding="utf-8",
    )
    return {
        "pipeline": pipeline_path,
        "retrieval": retrieval_path,
        "llm_tokenizer": llm_tokenizer,
        "embedding_tokenizer": embedding_tokenizer,
        "corpus_policy": corpus_policy,
    }


def _settings(tmp_path: Path, paths: dict[str, Path]) -> RuntimeSettings:
    return RuntimeSettings(
        access_mode=AccessMode.SHARED_CORPUS,
        query_token=uuid.uuid4().hex,
        admin_token=uuid.uuid4().hex,
        qdrant_api_key=uuid.uuid4().hex,
        qdrant_url="http://qdrant:6333",
        qdrant_alias="rag-active",
        release_revision="1" * 40,
        state_database=tmp_path / "state.sqlite3",
        manifest_database=tmp_path / "manifest.sqlite3",
        pipeline_path=paths["pipeline"],
        retrieval_path=paths["retrieval"],
        corpus_policy_path=paths["corpus_policy"],
        frontend_dir=tmp_path / "frontend",
        llm_tokenizer_path=paths["llm_tokenizer"],
        embedding_tokenizer_path=paths["embedding_tokenizer"],
        input_root=tmp_path / "docs",
        index_state_dir=tmp_path / "indexes",
        embedding_endpoints='["http://embedding:80"]',
        reranker_endpoints='["http://reranker:80"]',
        llm_endpoints='["http://llm:80"]',
        ocr_endpoints='["http://ocr:8090"]',
        embedding_model="embedding-model",
        reranker_model="reranker-model",
        llm_model="llm-model",
    )


def _refresh_freeze_decision(paths: dict[str, Path]) -> None:
    """在测试主动修改有效 pipeline 后同步冻结指纹。"""
    pipeline = load_pipeline(paths["pipeline"])
    retrieval = RetrievalSettings.load(paths["retrieval"])
    decision_path = paths["retrieval"].with_name("FREEZE_DECISION.json")
    decision = FreezeDecision.load(decision_path).model_copy(
        update={
            "index_fingerprint": pipeline.index_fingerprint(),
            "serving_fingerprint": retrieval.serving_fingerprint(pipeline),
        }
    )
    decision_path.write_text(
        json.dumps(decision.model_dump(mode="json")),
        encoding="utf-8",
    )
    payload = retrieval.model_dump(mode="json")
    payload["freeze_decision_sha256"] = decision.sha256()
    paths["retrieval"].write_text(json.dumps(payload), encoding="utf-8")


def _forbid_qdrant(
    calls: list[str],
) -> Callable[..., None]:
    def construct(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append("qdrant")
        raise AssertionError("preflight 失败后不得构造 Qdrant 客户端")

    return construct


@pytest.mark.parametrize(
    "installed_revision",
    ("", "development-unset", "2" * 40),
)
@pytest.mark.parametrize("builder", ("serve", "worker"))
def test_runtime_revision_mismatch_fails_before_external_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_revision: str,
    builder: str,
) -> None:
    paths = _write_configuration(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        runtime_module,
        "SOURCE_REVISION",
        installed_revision,
    )
    monkeypatch.setattr(
        runtime_module,
        "QdrantClient",
        _forbid_qdrant(calls),
    )
    monkeypatch.setattr(
        worker_runtime_module,
        "QdrantClient",
        _forbid_qdrant(calls),
    )

    with pytest.raises(ValueError, match="SOURCE_REVISION"):
        if builder == "serve":
            build_runtime(_settings(tmp_path, paths))
        else:
            build_worker_runtime(_settings(tmp_path, paths))

    assert calls == []
    assert not (tmp_path / "state.sqlite3").exists()
    assert not (tmp_path / "manifest.sqlite3").exists()


@pytest.mark.parametrize(
    "arguments",
    (
        ("serve",),
        ("worker", "--once"),
        ("index", "full", "--idempotency-key", "revision-test"),
    ),
)
def test_cli_runtime_commands_reject_revision_before_external_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
) -> None:
    paths = _write_configuration(tmp_path)
    settings = _settings(tmp_path, paths)
    calls: list[str] = []
    monkeypatch.setattr(runtime_module, "SOURCE_REVISION", "2" * 40)
    monkeypatch.setattr(cli_module, "RuntimeSettings", lambda: settings)
    monkeypatch.setattr(
        cli_module,
        "verify_offline_assets",
        lambda _paths: None,
    )
    monkeypatch.setattr(
        runtime_module,
        "QdrantClient",
        _forbid_qdrant(calls),
    )
    monkeypatch.setattr(
        worker_runtime_module,
        "QdrantClient",
        _forbid_qdrant(calls),
    )

    with pytest.raises(ValueError, match="SOURCE_REVISION"):
        cli_module.main(arguments)

    assert calls == []
    assert not (tmp_path / "state.sqlite3").exists()
    assert not (tmp_path / "manifest.sqlite3").exists()


@pytest.mark.parametrize(
    ("field", "value", "error_pattern"),
    (
        ("embedding_tokenizer_sha256", "0" * 64, "embedding tokenizer"),
        ("llm_tokenizer_sha256", "0" * 64, "LLM tokenizer"),
        ("prompt_revision", "sha256:" + "0" * 64, "prompt revision"),
    ),
)
def test_runtime_contract_mismatch_fails_before_network_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    error_pattern: str,
) -> None:
    paths = _write_configuration(tmp_path)
    payload = json.loads(paths["pipeline"].read_text(encoding="utf-8"))
    payload[field] = value
    paths["pipeline"].write_text(json.dumps(payload), encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        runtime_module,
        "QdrantClient",
        _forbid_qdrant(calls),
    )

    with pytest.raises(ValueError, match=error_pattern):
        build_runtime(_settings(tmp_path, paths))

    assert calls == []
    assert not (tmp_path / "state.sqlite3").exists()
    assert not (tmp_path / "manifest.sqlite3").exists()


@pytest.mark.parametrize(
    ("setting_name", "error_pattern"),
    (
        ("embedding_model", "embedding model"),
        ("reranker_model", "reranker model"),
        ("llm_model", "LLM model"),
    ),
)
def test_runtime_model_id_mismatch_fails_before_network_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting_name: str,
    error_pattern: str,
) -> None:
    paths = _write_configuration(tmp_path)
    settings = _settings(tmp_path, paths).model_copy(
        update={setting_name: "wrong-model"}
    )
    calls: list[str] = []
    monkeypatch.setattr(
        runtime_module,
        "QdrantClient",
        _forbid_qdrant(calls),
    )

    with pytest.raises(ValueError, match=error_pattern):
        build_runtime(settings)

    assert calls == []
    assert not (tmp_path / "state.sqlite3").exists()


def test_runtime_policy_digest_mismatch_fails_before_network_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_configuration(tmp_path)
    payload = json.loads(paths["pipeline"].read_text(encoding="utf-8"))
    payload["corpus_policy_sha256"] = "0" * 64
    paths["pipeline"].write_text(json.dumps(payload), encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        runtime_module,
        "QdrantClient",
        _forbid_qdrant(calls),
    )

    with pytest.raises(ValueError, match="corpus policy SHA256"):
        build_runtime(_settings(tmp_path, paths))

    assert calls == []
    assert not (tmp_path / "state.sqlite3").exists()


def test_runtime_rejects_v2_parser_before_external_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_configuration(tmp_path)
    payload = json.loads(paths["pipeline"].read_text(encoding="utf-8"))
    payload["parser_revision"] = "docx-parser-v2"
    paths["pipeline"].write_text(json.dumps(payload), encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        runtime_module,
        "QdrantClient",
        _forbid_qdrant(calls),
    )

    with pytest.raises(ValueError, match="parser revision"):
        build_runtime(_settings(tmp_path, paths))

    assert calls == []
    assert not (tmp_path / "state.sqlite3").exists()
    assert not (tmp_path / "manifest.sqlite3").exists()


@pytest.mark.parametrize(
    ("field", "value", "error_pattern"),
    (
        ("parser_revision", "docx-parser-v2", "parser revision"),
        ("sparse_revision", "sha256:" + "0" * 64, "BM25 revision"),
        ("sparse_tokenizer", "word", "BM25"),
        ("embedding_tokenizer_sha256", "0" * 64, "embedding tokenizer"),
        ("embedding_model", "wrong-model", "embedding model"),
    ),
)
def test_worker_contract_mismatch_fails_before_network_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    error_pattern: str,
) -> None:
    paths = _write_configuration(tmp_path)
    payload = json.loads(paths["pipeline"].read_text(encoding="utf-8"))
    payload[field] = value
    paths["pipeline"].write_text(json.dumps(payload), encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        worker_runtime_module,
        "QdrantClient",
        _forbid_qdrant(calls),
    )

    with pytest.raises(ValueError, match=error_pattern):
        build_worker_runtime(_settings(tmp_path, paths))

    assert calls == []
    assert not (tmp_path / "state.sqlite3").exists()
    assert not (tmp_path / "manifest.sqlite3").exists()


def test_worker_unknown_policy_override_fails_before_embedding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_configuration(tmp_path)
    input_root = tmp_path / "docs"
    input_root.mkdir()
    (input_root / "a.docx").write_bytes(b"synthetic")
    policy_payload = json.loads(
        paths["corpus_policy"].read_text(encoding="utf-8")
    )
    policy_payload["overrides"] = [
        {
            "path": "missing.docx",
            "document_status": "active",
            "authority_level": "official",
            "effective_from": None,
            "effective_to": None,
        }
    ]
    paths["corpus_policy"].write_text(
        json.dumps(policy_payload),
        encoding="utf-8",
    )
    pipeline_payload = json.loads(
        paths["pipeline"].read_text(encoding="utf-8")
    )
    pipeline_payload["corpus_policy_sha256"] = CorpusPolicy.load(
        paths["corpus_policy"]
    ).semantic_sha256()
    paths["pipeline"].write_text(
        json.dumps(pipeline_payload),
        encoding="utf-8",
    )
    calls: list[str] = []
    monkeypatch.setattr(
        worker_runtime_module,
        "QdrantClient",
        _forbid_qdrant(calls),
    )

    with pytest.raises(ValueError, match="未发现"):
        build_worker_runtime(_settings(tmp_path, paths))

    assert calls == []
    assert not (tmp_path / "state.sqlite3").exists()


def test_worker_uses_pipeline_document_embedding_instruction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_configuration(tmp_path)
    payload = json.loads(paths["pipeline"].read_text(encoding="utf-8"))
    payload["document_embedding_instruction"] = "固定文档向量指令"
    paths["pipeline"].write_text(json.dumps(payload), encoding="utf-8")
    _refresh_freeze_decision(paths)
    (tmp_path / "docs").mkdir()
    captured: list[str] = []

    def capture_builder(
        *,
        config: DocxBuildConfig,
        services: object,
    ) -> object:
        del services
        captured.append(config.embedding_instruction)
        return object()

    monkeypatch.setattr(
        worker_runtime_module,
        "DocxChunkBuilder",
        capture_builder,
    )
    bundle = build_worker_runtime(_settings(tmp_path, paths))
    try:
        services = bundle.runner._services
        services.build_chunks_factory(bundle.control)
    finally:
        bundle.close()

    assert captured == ["固定文档向量指令"]
