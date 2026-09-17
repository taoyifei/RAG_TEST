"""只用虚构最小对象探测当前湾事通内网 LLM 的协议能力。"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import BinaryIO

from rag_app.wanshitong.internal_model_settings import (
    InternalCredentialSettings,
    InternalModelSettings,
)

_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
_MESSAGES = [
    {"role": "system", "content": "只返回符合给定结构的虚构 JSON 对象。"},
    {"role": "user", "content": "虚构检查：返回 ok 为 true。"},
]
_MODE_ORDER = ("response_format", "structured_outputs", "guided_json")
_TIMEOUT_SECONDS = 20.0


def _credential(settings: InternalCredentialSettings) -> str:
    if settings.source == "environment":
        return os.environ.get(settings.environment_name or "", "")
    if settings.source == "file":
        return settings.database_secret()
    return ""


def _content(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("INVALID_RESPONSE")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("INVALID_RESPONSE")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("INVALID_RESPONSE")
    message = first.get("message")
    if not isinstance(message, dict) or not isinstance(
        message.get("content"), str
    ):
        raise ValueError("INVALID_RESPONSE")
    return message["content"]


def _mode_payload(mode: str) -> dict[str, object]:
    if mode == "response_format":
        return {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "fictional_probe",
                    "schema": _SCHEMA,
                    "strict": True,
                },
            }
        }
    if mode == "structured_outputs":
        return {"structured_outputs": {"json": _SCHEMA}}
    if mode == "guided_json":
        return {"guided_json": _SCHEMA}
    return {}


def _request(
    settings: InternalModelSettings,
    *,
    mode: str,
    disable_thinking: bool,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> tuple[bool, str, float]:
    payload: dict[str, object] = {
        "model": settings.llm_model,
        "messages": _MESSAGES,
        "temperature": 0,
        "max_tokens": 48,
        "stream": False,
        **_mode_payload(mode),
    }
    if disable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Content-Type": "application/json"}
    key = _credential(settings.llm_credential)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(  # noqa: S310
        settings.llm_base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers=headers,
        method="POST",
    )
    started = time.perf_counter()
    try:
        with opener(request, timeout=_TIMEOUT_SECONDS) as response:
            content = _content(json.load(response))
        if mode == "thinking":
            valid = "<think>" not in content.casefold()
        else:
            parsed = json.loads(content)
            valid = (
                isinstance(parsed, dict)
                and set(parsed) == {"ok"}
                and isinstance(parsed["ok"], bool)
            )
        status = "OK" if valid else "INVALID_OUTPUT"
    except urllib.error.HTTPError as error:
        status = f"HTTP_{error.code}"
        valid = False
    except (urllib.error.URLError, TimeoutError):
        status = "TRANSPORT_ERROR"
        valid = False
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        status = "INVALID_RESPONSE"
        valid = False
    return valid, status, round((time.perf_counter() - started) * 1000, 2)


def probe(
    settings: InternalModelSettings,
    *,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> Mapping[str, object]:
    """不暴露地址、凭据、Prompt 或响应正文的能力摘要。"""
    thinking, thinking_status, thinking_ms = _request(
        settings, mode="thinking", disable_thinking=True, opener=opener
    )
    checks = {"thinking": thinking_status}
    latencies = {"thinking": thinking_ms}
    supported: list[str] = []
    for mode in _MODE_ORDER:
        valid, status, elapsed_ms = _request(
            settings, mode=mode, disable_thinking=thinking, opener=opener
        )
        checks[mode] = status
        latencies[mode] = elapsed_ms
        if valid:
            supported.append(mode)
    return {
        "model": settings.llm_model,
        "base_url_sha256": hashlib.sha256(
            settings.llm_base_url.encode()
        ).hexdigest(),
        "thinking_disable_supported": thinking,
        "structured_output_modes_supported": supported,
        "selected_structured_output_mode": (
            supported[0] if supported else "none"
        ),
        "latency_ms": latencies,
        "probe_status": "OK" if thinking or supported else "UNSUPPORTED",
        "checks": checks,
    }


def main() -> None:
    """读取候选现有配置并只输出脱敏能力摘要。"""
    settings = InternalModelSettings.from_environment()
    print(json.dumps(probe(settings), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
