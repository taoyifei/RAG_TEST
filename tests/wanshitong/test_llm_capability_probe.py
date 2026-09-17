"""LLM 能力探测只报告安全元数据，协议逐项独立试探。"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

from rag_app.wanshitong.internal_model_settings import InternalModelSettings
from scripts.probe_wanshitong_llm_capabilities import probe


def _settings() -> InternalModelSettings:
    return InternalModelSettings(
        embedding_base_url="http://127.0.0.1:18001/v1",
        reranker_base_url="http://127.0.0.1:18002",
        llm_base_url="http://private-model.invalid/v1",
    )


def test_probe_selects_supported_mode_without_exposing_model_body() -> None:
    requests: list[dict[str, object]] = []

    def _open(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        assert timeout > 0
        payload = json.loads(request.data or b"{}")
        requests.append(payload)
        if "guided_json" in payload:
            raise urllib.error.HTTPError(request.full_url, 400, "", None, None)
        response = {"choices": [{"message": {"content": '{"ok":true}'}}]}
        return io.BytesIO(json.dumps(response).encode())

    result = probe(_settings(), opener=_open)
    serialized = json.dumps(result, ensure_ascii=False)

    assert len(requests) == 4
    assert result["thinking_disable_supported"] is True
    assert result["selected_structured_output_mode"] == "response_format"
    assert result["structured_output_modes_supported"] == [
        "response_format",
        "structured_outputs",
    ]
    assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "private-model.invalid" not in serialized
    assert "虚构检查" not in serialized
    assert "ok" not in serialized


def test_probe_rejects_non_schema_output() -> None:
    def _open(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        del request, timeout
        response = {"choices": [{"message": {"content": "不是 JSON"}}]}
        return io.BytesIO(json.dumps(response).encode())

    result = probe(_settings(), opener=_open)
    assert result["structured_output_modes_supported"] == []
    assert result["selected_structured_output_mode"] == "none"
