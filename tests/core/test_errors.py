import pytest

from rag_app.core.errors import (
    ConfigurationError,
    ProviderUnavailable,
    RagError,
)


def test_error_string_contains_only_code_and_safe_message() -> None:
    error = ProviderUnavailable(
        "上游暂时不可用。",
        stage="embedding",
        details={"provider": "jina", "status": 503},
    )
    rendered = str(error)
    assert rendered == "PROVIDER_UNAVAILABLE: 上游暂时不可用。"
    assert "503" not in rendered
    assert error.retryable is True
    assert error.stage == "embedding"


@pytest.mark.parametrize(
    "details",
    [
        {"api_key": "example"},
        {"response_body": "raw"},
        {"sql": "SELECT secret"},
        {"document_text": "private"},
        {"file_path": "/private/document.docx"},
        {"nested": {"api_key": "example"}},
    ],
)
def test_error_rejects_sensitive_detail_fields(details: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        ConfigurationError("配置无效。", stage="config", details=details)


def test_all_public_errors_keep_stable_machine_fields() -> None:
    error = RagError(
        "安全说明。",
        stage="test",
        code="CUSTOM_CODE",
        retryable=True,
        trace_id=f"trace_{'a' * 32}",
    )
    assert error.code == "CUSTOM_CODE"
    assert error.safe_message == "安全说明。"
    assert error.retryable is True
    assert error.trace_id == f"trace_{'a' * 32}"
