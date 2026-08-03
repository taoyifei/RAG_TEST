import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from rag_app.health import FrozenConfigurationProbe
from rag_app.settings import RetrievalSettings, RuntimeSettings


def _runtime_values(tmp_path: Path) -> dict[str, object]:
    return {
        "access_mode": "shared_corpus",
        "query_token": uuid.uuid4().hex,
        "admin_token": uuid.uuid4().hex,
        "qdrant_api_key": uuid.uuid4().hex,
        "qdrant_url": "http://rag-qdrant:6333",
        "qdrant_alias": "rag-docx-active",
        "release_revision": "1" * 40,
        "state_database": tmp_path / "state.sqlite3",
        "manifest_database": tmp_path / "manifest.sqlite3",
        "pipeline_path": tmp_path / "pipeline.json",
        "retrieval_path": tmp_path / "retrieval.json",
        "frontend_dir": tmp_path / "frontend",
        "llm_tokenizer_path": tmp_path / "tokenizer.json",
        "embedding_endpoints": '["http://embedding:80"]',
        "reranker_endpoints": '["http://reranker:80"]',
        "llm_endpoints": '["http://llm-1:8000","http://llm-2:8000"]',
        "embedding_model": "Qwen3-Embedding-0.6B",
        "reranker_model": "Qwen3-Reranker-0.6B",
        "llm_model": "Qwen/Qwen3-8B-AWQ",
    }


def test_runtime_settings_require_explicit_access_mode(
    tmp_path: Path,
) -> None:
    values = _runtime_values(tmp_path)
    values.pop("access_mode")
    with pytest.raises(ValidationError, match="access_mode"):
        RuntimeSettings(**values)


def test_runtime_settings_accept_only_shared_corpus(
    tmp_path: Path,
) -> None:
    values = _runtime_values(tmp_path)
    values["access_mode"] = "shared_corpus"

    settings = RuntimeSettings(**values)

    assert settings.access_mode == "shared_corpus"
    values["access_mode"] = "permissioned"
    with pytest.raises(ValidationError, match="access_mode"):
        RuntimeSettings(**values)


def test_runtime_settings_require_distinct_non_default_secrets(
    tmp_path: Path,
) -> None:
    values = _runtime_values(tmp_path)
    values["admin_token"] = values["query_token"]

    with pytest.raises(ValidationError, match="必须不同"):
        RuntimeSettings(**values)

    values.pop("admin_token")
    with pytest.raises(ValidationError, match="admin_token"):
        RuntimeSettings(**values)


def test_runtime_settings_parse_bounded_endpoint_lists(
    tmp_path: Path,
) -> None:
    settings = RuntimeSettings(**_runtime_values(tmp_path))

    assert settings.embedding_endpoint_urls() == ("http://embedding:80",)
    assert settings.reranker_endpoint_urls() == ("http://reranker:80",)
    assert settings.llm_endpoint_urls() == (
        "http://llm-1:8000",
        "http://llm-2:8000",
    )
    assert settings.query_token.get_secret_value() not in repr(settings)
    assert settings.max_embedding_concurrency == 4
    assert settings.max_reranker_concurrency == 4
    assert settings.max_llm_concurrency == 4
    assert settings.max_ocr_concurrency == 1
    assert not hasattr(settings, "max_model_concurrency")


def test_runtime_settings_reject_endpoint_with_api_path(
    tmp_path: Path,
) -> None:
    values = _runtime_values(tmp_path)
    values["llm_endpoints"] = '["http://llm-1:8000/v1"]'

    with pytest.raises(ValidationError, match="无路径"):
        RuntimeSettings(**values)


@pytest.mark.parametrize(
    "revision",
    (None, "1" * 12, "A" * 40, "0.1.0"),
)
def test_runtime_settings_require_full_lowercase_git_revision(
    tmp_path: Path,
    revision: str | None,
) -> None:
    values = _runtime_values(tmp_path)
    if revision is None:
        values.pop("release_revision")
    else:
        values["release_revision"] = revision

    with pytest.raises(ValidationError, match="release_revision"):
        RuntimeSettings(**values)


def test_provisional_retrieval_config_is_not_ready(tmp_path: Path) -> None:
    path = tmp_path / "retrieval.json"
    path.write_text(
        json.dumps(
            {
                "status": "provisional",
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
                    "allowed_statuses": ["active"],
                    "allowed_authority_levels": ["official"],
                }
        ),
        encoding="utf-8",
    )
    settings = RetrievalSettings.load(path)

    status = FrozenConfigurationProbe(settings).check()

    assert status.ready is False
    assert status.detail == "retrieval parameters are not frozen"
