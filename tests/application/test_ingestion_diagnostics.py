"""V3-00.6 入库阶段终态与脱敏诊断回归。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from rag_app.adapters.legacy.stores import SqliteTraceSink
from rag_app.application.revision_builder import IngestionDocument
from rag_app.composition.p06_runtime import P06Runtime
from rag_app.core.errors import JobCancelled, RagError
from rag_app.core.events import TraceEvent
from rag_app.core.identifiers import (
    deterministic_id,
    document_version_id,
)
from rag_app.core.models import DocumentRef
from tests.adapters.parsers.docx.fixtures import build_package
from tests.persistence.helpers import runtime_with_kb

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


class _UnsafeValidationModel(BaseModel):
    """制造多条错误、控制字符和私有 input 的测试模型。"""

    model_config = ConfigDict(title="Chunk\nTRACE_TITLE_INJECTION")

    field_0: int = Field(alias="正文\nTRACE_PATH_INJECTION")
    field_1: int
    field_2: int
    field_3: int
    field_4: int
    field_5: int
    field_6: int
    field_7: int
    field_8: int
    field_9: int


class _SafeChunkError(ValueError):
    """模拟 Adapter 对底层 ValidationError 的安全身份补充。"""

    def __init__(self) -> None:
        self.node_id = f"node_{'1' * 32}"
        self.chunk_id = f"chunk_{'2' * 32}"
        super().__init__("安全模型不变量失败。")


class _UnsafeRootValidationModel(BaseModel):
    """制造携带私有正文的模型级根路径错误。"""

    payload: str

    @model_validator(mode="after")
    def _reject_payload(self) -> NoReturn:
        """拒绝输入并把正文放入第三方原始错误消息。

        Args:
            无参数；读取当前测试模型。

        Returns:
            不返回；始终抛出模型级错误。

        """
        raise ValueError(f"PRIVATE_ROOT_BODY:{self.payload}")


def _document(
    project_id: str,
    knowledge_base_id: str,
    document_id: str,
    text: str,
) -> IngestionDocument:
    return IngestionDocument(
        document=DocumentRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            display_name="通用诊断样例.docx",
        ),
        content=build_package(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"),
        media_type=_MEDIA_TYPE,
    )


def _build_ids(
    index_fingerprint: str,
    knowledge_base_id: str,
    document: IngestionDocument,
) -> tuple[str, str]:
    digest = hashlib.sha256(document.content).hexdigest()
    version_id = document_version_id(document.document.document_id, digest)
    revision_id = deterministic_id(
        "irev",
        knowledge_base_id,
        (version_id,),
        index_fingerprint,
    )
    return revision_id, deterministic_id("job", knowledge_base_id, revision_id)


def _validation_failure() -> NoReturn:
    values = {
        "正文\nTRACE_PATH_INJECTION": "PRIVATE_SOURCE_BODY",
        **{f"field_{index}": "PRIVATE_SOURCE_BODY" for index in range(1, 10)},
    }
    _UnsafeValidationModel.model_validate(values)
    raise AssertionError("测试模型必须产生 ValidationError。")


def _wrapped_validation_failure() -> NoReturn:
    try:
        _validation_failure()
    except ValidationError as error:
        raise _SafeChunkError() from error


def _root_validation_failure() -> NoReturn:
    _UnsafeRootValidationModel.model_validate(
        {"payload": "PRIVATE_ROOT_BODY\nTRACE_ROOT_INJECTION"}
    )
    raise AssertionError("测试模型必须产生根级 ValidationError。")


def _event_payload(event: TraceEvent) -> dict[str, object]:
    return dict(json.loads(event.model_dump_json()))


def _trace_events(runtime: P06Runtime, trace_id: str) -> tuple[TraceEvent, ...]:
    sink = runtime.components.trace_sink
    assert isinstance(sink, SqliteTraceSink)
    return sink.events(trace_id)


def test_validation_diagnostic_is_bounded_safe_and_keeps_old_active(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型级错误保留应用帧、有限错误与失败终态，旧索引不切换。"""
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    document_id = deterministic_id("doc", knowledge_base_id, "diagnostic")
    original = _document(
        project_id,
        knowledge_base_id,
        document_id,
        "设备 AX-17 的巡检周期为十一天。",
    )
    changed = _document(
        project_id,
        knowledge_base_id,
        document_id,
        "设备 AX-17 的巡检周期调整为十二天。",
    )
    try:
        succeeded = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(original,),
            idempotency_key="diagnostic-success",
            budgets=runtime.default_budgets(),
        )
        original_trace = deterministic_id(
            "trace", succeeded.job_id, succeeded.revision_id
        )
        success_terminals = [
            _event_payload(event)
            for event in _trace_events(runtime, original_trace)
            if event.event_name.endswith(".finished")
        ]
        assert success_terminals
        assert all(
            dict(item["attributes"])["status"] == "success"
            for item in success_terminals
        )

        chunker_type = type(runtime.components.chunker)

        def fail_chunk(*_args: object, **_kwargs: object) -> NoReturn:
            _wrapped_validation_failure()

        monkeypatch.setattr(chunker_type, "chunk", fail_chunk)
        revision_id, job_id = _build_ids(
            runtime.components.index_fingerprint,
            knowledge_base_id,
            changed,
        )
        with pytest.raises(RagError) as captured:
            runtime.builder.build_and_activate(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                documents=(changed,),
                idempotency_key="diagnostic-failure",
                budgets=runtime.default_budgets(),
            )

        assert captured.value.code == "CHUNKING_FAILED"
        assert runtime.control.active_revision_id(knowledge_base_id) == (
            succeeded.revision_id
        )
        summary = runtime.control.job_summary(job_id)
        assert summary["state"] == "failed_terminal"
        assert summary["stage"] == "chunking"
        events = _trace_events(
            runtime, deterministic_id("trace", job_id, revision_id)
        )
        started = next(
            _event_payload(event)
            for event in events
            if event.event_name == "ingestion.chunking.started"
        )
        started_attributes = dict(started["attributes"])
        assert (
            dict(started_attributes["chunking_policy"])["hard_max_tokens"]
            == 512
        )
        assert dict(started_attributes["tokenizer"])["tokenizer_id"] == (
            "deterministic-utf8-v1"
        )
        slot_limits = list(started_attributes["embedding_slot_limits"])
        assert {dict(item)["slot_id"] for item in slot_limits} == {"primary"}
        assert all(dict(item)["max_input_tokens"] > 0 for item in slot_limits)

        terminals = [
            _event_payload(event)
            for event in events
            if event.event_name == "ingestion.chunking.finished"
        ]
        assert len(terminals) == 1
        terminal_attributes = dict(terminals[0]["attributes"])
        assert terminal_attributes["status"] == "failed"
        assert terminal_attributes["error_code"] == "CHUNKING_FAILED"
        failure = next(
            _event_payload(event)
            for event in events
            if event.event_name == "ingestion.failed"
        )
        details = dict(dict(failure["attributes"])["details"])
        assert details["error_type"] == "pydantic_validation"
        assert details["invariant_code"] == "PYDANTIC_VALIDATION_FAILED"
        assert details["model_name"] == "pydantic_model"
        assert details["validation_error_count"] == 10
        assert details["validation_errors_truncated"] is True
        assert details["node_id"] == f"node_{'1' * 32}"
        assert details["chunk_id"] == f"chunk_{'2' * 32}"
        assert details["cause_types"] == [
            "_SafeChunkError",
            "ValidationError",
        ]
        validation_errors = list(details["validation_errors"])
        assert len(validation_errors) == 8
        assert dict(validation_errors[0])["path"] == "$[?]"
        assert dict(validation_errors[0])["safe_reason"]
        assert details["invalid_fields"][0] == "$[?]"
        assert dict(details["application_frame"])["file"].startswith(
            "src/rag_app/"
        )
        assert dict(details["origin_frame"])["file"] == "main.py"
        serialized = json.dumps(failure, ensure_ascii=False)
        assert "PRIVATE_SOURCE_BODY" not in serialized
        assert "TRACE_TITLE_INJECTION" not in serialized
        assert "TRACE_PATH_INJECTION" not in serialized
        assert "\\n" not in serialized
    finally:
        runtime.close()


def test_cancelled_stage_has_one_cancelled_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消与失败分开编码，并且 chunking 阶段只有一个终态。"""
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    document = _document(
        project_id,
        knowledge_base_id,
        deterministic_id("doc", knowledge_base_id, "cancelled"),
        "独立取消样例。",
    )

    def cancel_chunk(*_args: object, **_kwargs: object) -> NoReturn:
        raise JobCancelled("作业已取消。", stage="job.cancel_check")

    try:
        monkeypatch.setattr(
            type(runtime.components.chunker), "chunk", cancel_chunk
        )
        revision_id, job_id = _build_ids(
            runtime.components.index_fingerprint,
            knowledge_base_id,
            document,
        )
        with pytest.raises(JobCancelled):
            runtime.builder.build_and_activate(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                documents=(document,),
                idempotency_key="cancelled-stage",
                budgets=runtime.default_budgets(),
            )
        events = _trace_events(
            runtime, deterministic_id("trace", job_id, revision_id)
        )
        terminals = [
            _event_payload(event)
            for event in events
            if event.event_name == "ingestion.chunking.finished"
        ]
        assert len(terminals) == 1
        attributes = dict(terminals[0]["attributes"])
        assert attributes["status"] == "cancelled"
        assert attributes["error_code"] == "JOB_CANCELLED"
    finally:
        runtime.close()


def test_retry_attempt_uses_a_new_trace_root(tmp_path: Path) -> None:
    """第二次尝试保留第一次 Trace，并把全部新事件写入新根。"""
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    document = _document(
        project_id,
        knowledge_base_id,
        deterministic_id("doc", knowledge_base_id, "retry-trace"),
        "第二次尝试的公开合成样例。",
    )
    revision_id, job_id = _build_ids(
        runtime.components.index_fingerprint,
        knowledge_base_id,
        document,
    )
    first_trace_id = deterministic_id("trace", job_id, revision_id)
    second_trace_id = deterministic_id("trace", job_id, revision_id, 2)
    sink = runtime.components.trace_sink
    assert isinstance(sink, SqliteTraceSink)
    sink.record(
        TraceEvent(
            trace_id=first_trace_id,
            event_name="ingestion.failed",
            occurred_at=datetime.now(UTC),
            attributes={"attempt": 1, "job_id": job_id},
        )
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(document,),
            idempotency_key="retry-trace-attempt-two",
            budgets=runtime.default_budgets(),
            attempt=2,
            persistent_job_id=job_id,
        )
        assert result.job_id == job_id
        assert [
            event.event_name for event in _trace_events(runtime, first_trace_id)
        ] == ["ingestion.failed"]
        retry_events = _trace_events(runtime, second_trace_id)
        assert retry_events
        assert retry_events[0].event_name == "ingestion.started"
        assert retry_events[-1].event_name == "ingestion.completed"
        assert all(
            dict(event.attributes)["attempt"] == 2 for event in retry_events
        )
    finally:
        runtime.close()


def test_model_level_validation_path_is_explicit_root_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型级 loc=() 显示为 `$`，且原始消息和 input 均不外泄。"""
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    document = _document(
        project_id,
        knowledge_base_id,
        deterministic_id("doc", knowledge_base_id, "root-diagnostic"),
        "独立模型级诊断样例。",
    )

    def fail_chunk(*_args: object, **_kwargs: object) -> NoReturn:
        _root_validation_failure()

    try:
        monkeypatch.setattr(
            type(runtime.components.chunker), "chunk", fail_chunk
        )
        revision_id, job_id = _build_ids(
            runtime.components.index_fingerprint,
            knowledge_base_id,
            document,
        )
        with pytest.raises(RagError):
            runtime.builder.build_and_activate(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                documents=(document,),
                idempotency_key="root-diagnostic",
                budgets=runtime.default_budgets(),
            )
        events = _trace_events(
            runtime, deterministic_id("trace", job_id, revision_id)
        )
        failure = next(
            _event_payload(event)
            for event in events
            if event.event_name == "ingestion.failed"
        )
        details = dict(dict(failure["attributes"])["details"])
        assert details["invalid_fields"] == ["$"]
        validation_errors = list(details["validation_errors"])
        assert validation_errors == [
            {
                "path": "$",
                "error_type": "value_error",
                "invariant_code": "PYDANTIC_MODEL_INVARIANT_FAILED",
                "safe_reason": "模型级不变量未通过。",
            }
        ]
        serialized = json.dumps(failure, ensure_ascii=False)
        assert "PRIVATE_ROOT_BODY" not in serialized
        assert "TRACE_ROOT_INJECTION" not in serialized
        assert "\\n" not in serialized
    finally:
        runtime.close()


def test_terminal_trace_failure_does_not_replace_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """阶段收尾记录失败只能附注，不能覆盖原始 ValidationError。"""
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    document = _document(
        project_id,
        knowledge_base_id,
        deterministic_id("doc", knowledge_base_id, "trace-cleanup"),
        "收尾失败样例。",
    )
    sink = runtime.components.trace_sink
    original_record = sink.record

    def record(event: TraceEvent) -> None:
        if event.event_name == "ingestion.chunking.finished":
            raise RuntimeError("TRACE_TERMINAL_FAILURE")
        original_record(event)

    def fail_chunk(*_args: object, **_kwargs: object) -> NoReturn:
        _validation_failure()

    try:
        monkeypatch.setattr(
            type(runtime.components.chunker), "chunk", fail_chunk
        )
        monkeypatch.setattr(sink, "record", record)
        with pytest.raises(RagError) as captured:
            runtime.builder.build_and_activate(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                documents=(document,),
                idempotency_key="trace-cleanup",
                budgets=runtime.default_budgets(),
            )
        assert captured.value.code == "CHUNKING_FAILED"
        assert isinstance(captured.value.__cause__, ValidationError)
        assert any(
            "RuntimeError" in note
            for note in getattr(captured.value, "__notes__", ())
        )
    finally:
        runtime.close()
