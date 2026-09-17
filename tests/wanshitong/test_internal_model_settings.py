"""湾事通内网模型环境配置的边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.wanshitong.internal_model_settings import InternalModelSettings


def _environment() -> dict[str, str]:
    return {
        "RAG_PRODUCT_MODE": "wanshitong",
        "RAG_WANSHITONG_DEMO_ALLOW_HTTP": "true",
        "RAG_WANSHITONG_EMBEDDING_BASE_URL": "http://embed.local/v1",
        "RAG_WANSHITONG_RERANKER_BASE_URL": "http://rerank.local",
        "RAG_WANSHITONG_LLM_BASE_URL": "http://llm.local/v1",
    }


def test_internal_settings_require_explicit_wanshitong_http_mode() -> None:
    environment = _environment()
    environment["RAG_WANSHITONG_DEMO_ALLOW_HTTP"] = "false"

    with pytest.raises(ValueError, match="DEMO_ALLOW_HTTP"):
        InternalModelSettings.from_environment(environment)

    environment["RAG_WANSHITONG_DEMO_ALLOW_HTTP"] = "true"
    environment["RAG_PRODUCT_MODE"] = "universal"
    with pytest.raises(ValueError, match="仅允许"):
        InternalModelSettings.from_environment(environment)


def test_internal_model_settings_normalize_primary_contracts() -> None:
    settings = InternalModelSettings.from_environment(_environment())

    assert settings.embedding_base_url == "http://embed.local/v1"
    assert settings.reranker_base_url == "http://rerank.local"
    assert settings.llm_base_url == "http://llm.local/v1"
    assert settings.embedding_dimension == 1024
    assert settings.reranker_protocol == "tei"
    assert settings.reranker_path == "/rerank"
    assert settings.embedding_credential.source == "none"
    assert not settings.llm_disable_thinking_supported
    assert settings.llm_structured_output_mode == "none"


def test_internal_settings_require_explicit_structured_output_mode() -> None:
    environment = _environment()
    environment["RAG_WANSHITONG_LLM_STRUCTURED_OUTPUT_MODE"] = (
        "response_format"
    )
    assert (
        InternalModelSettings.from_environment(
            environment
        ).llm_structured_output_mode
        == "response_format"
    )

    environment["RAG_WANSHITONG_LLM_STRUCTURED_OUTPUT_MODE"] = "auto"
    with pytest.raises(ValueError, match="STRUCTURED_OUTPUT_MODE"):
        InternalModelSettings.from_environment(environment)


def test_internal_settings_require_explicit_thinking_capability() -> None:
    environment = _environment()
    environment["RAG_WANSHITONG_LLM_DISABLE_THINKING_SUPPORTED"] = "true"
    assert InternalModelSettings.from_environment(
        environment
    ).llm_disable_thinking_supported

    environment["RAG_WANSHITONG_LLM_DISABLE_THINKING_SUPPORTED"] = "maybe"
    with pytest.raises(ValueError, match="必须为 true 或 false"):
        InternalModelSettings.from_environment(environment)


def test_internal_model_settings_support_safe_credential_sources(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "llm-api-key"
    secret_file.write_text("synthetic-secret\n", encoding="utf-8")
    secret_file.chmod(0o600)
    environment = _environment()
    environment["RAG_WANSHITONG_EMBEDDING_CREDENTIAL_ENV"] = (
        "RAG_WANSHITONG_EMBEDDING_API_KEY"
    )
    environment["RAG_WANSHITONG_LLM_API_KEY_FILE"] = str(secret_file)

    settings = InternalModelSettings.from_environment(environment)

    assert settings.embedding_credential.source == "environment"
    assert settings.embedding_credential.environment_name == (
        "RAG_WANSHITONG_EMBEDDING_API_KEY"
    )
    assert settings.llm_credential.source == "file"
    assert settings.llm_credential.database_secret() == "synthetic-secret"


def test_internal_model_settings_reject_ambiguous_or_unsafe_credentials(
    tmp_path: Path,
) -> None:
    environment = _environment()
    environment["RAG_WANSHITONG_LLM_CREDENTIAL_ENV"] = "RAG_LLM_API_KEY"
    environment["RAG_WANSHITONG_LLM_API_KEY_FILE"] = str(tmp_path / "key")
    with pytest.raises(ValueError, match="只能配置一种"):
        InternalModelSettings.from_environment(environment)

    environment = _environment()
    secret_file = tmp_path / "wide-key"
    secret_file.write_text("synthetic-secret", encoding="utf-8")
    secret_file.chmod(0o644)
    environment["RAG_WANSHITONG_LLM_API_KEY_FILE"] = str(secret_file)
    settings = InternalModelSettings.from_environment(environment)
    with pytest.raises(ValueError, match="0600"):
        settings.llm_credential.database_secret()


def test_internal_model_settings_reject_endpoint_arrays() -> None:
    environment = _environment()
    environment["RAG_WANSHITONG_EMBEDDING_BASE_URL"] = (
        '["http://embed-a.local/v1", "http://embed-b.local/v1"]'
    )

    with pytest.raises(ValueError, match="Endpoint 数组"):
        InternalModelSettings.from_environment(environment)
