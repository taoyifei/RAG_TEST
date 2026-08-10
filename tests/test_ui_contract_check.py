from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).parents[1]
_TRACE_ID = "a" * 32


def _module() -> ModuleType:
    path = (
        _ROOT / "deployment" / "industry" / "ui_contract_check.py"
    )
    spec = importlib.util.spec_from_file_location("ui_contract_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ndjson_requires_one_final_event_with_the_same_trace_id() -> None:
    module = _module()
    complete = (
        f'{{"trace_id":"{_TRACE_ID}","type":"answer_start"}}\n'
        f'{{"trace_id":"{_TRACE_ID}","type":"final"}}\n'
    ).encode()
    assert module._trace_id(complete) == _TRACE_ID

    incomplete = (
        f'{{"trace_id":"{_TRACE_ID}","type":"answer_start"}}\n'
    ).encode()
    with pytest.raises(module.UiContractError, match="FINAL"):
        module._trace_id(incomplete)

    duplicate_final = complete + (
        f'{{"trace_id":"{_TRACE_ID}","type":"final"}}\n'
    ).encode()
    with pytest.raises(module.UiContractError, match="FINAL"):
        module._trace_id(duplicate_final)

    after_final = complete + (
        f'{{"trace_id":"{_TRACE_ID}","type":"stage"}}\n'
    ).encode()
    with pytest.raises(module.UiContractError, match="FINAL"):
        module._trace_id(after_final)


def test_log_verification_checks_question_and_both_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    verify_log = getattr(module, "verify_log", None)
    assert callable(verify_log)
    monkeypatch.setenv("RAG_RUNTIME_CHECK_TOKEN", "q" * 32)
    monkeypatch.setenv("RAG_RUNTIME_ADMIN_TOKEN", "a" * 32)
    log_path = tmp_path / "app.log"
    log_path.write_text("safe application log\n", encoding="utf-8")
    assert verify_log(log_path) == {"log_redaction": "verified"}

    for secret in (module._QUESTION, "q" * 32, "a" * 32):
        log_path.write_text(f"unsafe={secret}\n", encoding="utf-8")
        with pytest.raises(module.UiContractError):
            verify_log(log_path)


def test_verify_script_captures_logs_after_ui_requests() -> None:
    source = (
        _ROOT / "deployment" / "industry" / "verify-app-update.sh"
    ).read_text(encoding="utf-8")

    request = source.index("verify-ui-trace")
    capture = source.index("docker logs --since")
    verification = source.index("verify-log")
    assert request < capture < verification
    assert "--log-path" in source
    assert os.linesep not in "verify-ui-trace"


def test_trace_visibility_polling_waits_for_terminal_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    responses = iter(
        (
            (200, {}, b'{"items":[]}'),
            (
                200,
                {},
                json.dumps(
                    {
                        "items": [
                            {
                                "question_preview": "",
                                "status": "RUNNING",
                                "trace_id": _TRACE_ID,
                            }
                        ]
                    }
                ).encode(),
            ),
            (404, {}, b'{}'),
            (
                200,
                {},
                json.dumps(
                    {
                        "items": [
                            {
                                "question_preview": module._QUESTION[:12],
                                "status": "ANSWERED",
                                "trace_id": _TRACE_ID,
                            }
                        ]
                    }
                ).encode(),
            ),
            (
                200,
                {},
                json.dumps(
                    {
                        "trace": {
                            "question_sha256": hashlib.sha256(
                                module._QUESTION.encode()
                            ).hexdigest(),
                            "question_text": module._QUESTION,
                            "status": "ANSWERED",
                            "trace_id": _TRACE_ID,
                        }
                    }
                ).encode(),
            ),
        )
    )
    monkeypatch.setattr(
        module, "_request", lambda *_args, **_kwargs: next(responses)
    )
    clock = {"now": 0.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    report = module._wait_for_trace_visibility(
        "http://127.0.0.1:8188",
        _TRACE_ID,
        {"Authorization": "Bearer redacted"},
        timeout_seconds=15.0,
        poll_interval_seconds=0.2,
    )

    assert report["trace_visibility_attempts"] == 3
    assert report["trace_visibility_elapsed_ms"] == 400
    assert "question" not in json.dumps(report)


@pytest.mark.parametrize(
    ("status", "body", "error"),
    (
        (401, b'{}', "ADMIN_TRACE_LIST_FAILED"),
        (422, b'{}', "ADMIN_TRACE_LIST_FAILED"),
        (500, b'{}', "ADMIN_TRACE_LIST_FAILED"),
        (
            200,
            b'{"items":[{"trace_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}]}',
            "TRACE_LIST_IDENTITY_INVALID",
        ),
    ),
)
def test_trace_visibility_polling_fails_closed_on_non_eventual_errors(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
    error: str,
) -> None:
    module = _module()
    monkeypatch.setattr(
        module, "_request", lambda *_args, **_kwargs: (status, {}, body)
    )

    with pytest.raises(module.UiContractError, match=error):
        module._wait_for_trace_visibility(
            "http://127.0.0.1:8188",
            _TRACE_ID,
            {"Authorization": "Bearer redacted"},
            timeout_seconds=0.01,
            poll_interval_seconds=0.1,
        )


def test_trace_visibility_polling_times_out_without_leaking_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(
        module, "_request", lambda *_args, **_kwargs: (200, {}, b'{"items":[]}')
    )
    clock = {"now": 0.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    with pytest.raises(
        module.UiContractError, match="TRACE_VISIBILITY_TIMEOUT"
    ):
        module._wait_for_trace_visibility(
            "http://127.0.0.1:8188",
            _TRACE_ID,
            {"Authorization": "Bearer redacted"},
            timeout_seconds=0.4,
            poll_interval_seconds=0.2,
        )


@pytest.mark.parametrize(
    ("question_text", "question_sha256", "error"),
    (
        ("错误问题", hashlib.sha256("错误问题".encode()).hexdigest(), "TEXT"),
        (None, "0" * 64, "SHA256"),
    ),
)
def test_trace_visibility_rejects_wrong_terminal_question_identity(
    monkeypatch: pytest.MonkeyPatch,
    question_text: str | None,
    question_sha256: str,
    error: str,
) -> None:
    module = _module()
    actual_question = (
        module._QUESTION if question_text is None else question_text
    )
    responses = iter(
        (
            (
                200,
                {},
                json.dumps(
                    {
                        "items": [
                            {
                                "question_preview": module._QUESTION[:12],
                                "status": "ANSWERED",
                                "trace_id": _TRACE_ID,
                            }
                        ]
                    }
                ).encode(),
            ),
            (
                200,
                {},
                json.dumps(
                    {
                        "trace": {
                            "question_sha256": question_sha256,
                            "question_text": actual_question,
                            "status": "ANSWERED",
                            "trace_id": _TRACE_ID,
                        }
                    }
                ).encode(),
            ),
        )
    )
    monkeypatch.setattr(
        module, "_request", lambda *_args, **_kwargs: next(responses)
    )

    with pytest.raises(module.UiContractError, match=error):
        module._wait_for_trace_visibility(
            "http://127.0.0.1:8188",
            _TRACE_ID,
            {"Authorization": "Bearer redacted"},
        )
