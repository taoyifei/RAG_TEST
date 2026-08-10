from __future__ import annotations

import importlib.util
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
