import io
import json
import logging

from rag_app.clients.model_services import ExternalCallAudit
from rag_app.generation.answer import (
    AnswerClaim,
    AnswerResult,
    AnswerStatus,
    ClaimSupport,
)
from rag_app.observability import StructuredAuditLogger
from rag_app.query_service import QueryOutcome, StageEvent, StageName


def test_structured_audit_logs_only_allowlisted_metadata() -> None:
    output = io.StringIO()
    logger = logging.getLogger("rag-test-audit")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(output))
    audit = StructuredAuditLogger(
        logger,
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    )
    audit.query_stage(
        StageEvent(
            trace_id="trace-1",
            stage=StageName.RERANK,
            elapsed_ms=31,
            metrics={"candidate_count": 6},
        )
    )
    audit.query_outcome(
        QueryOutcome(
            trace_id="trace-1",
            answer=AnswerResult(
                status=AnswerStatus.ANSWERED,
                answer="绝不能进入日志的回答",
                claims=(
                    AnswerClaim(
                        text="绝不能进入日志的事实",
                        supports=(
                            ClaimSupport(
                                evidence_id="E1",
                                chunk_id="chunk-1",
                                quote="绝不能进入日志的原文",
                                locator="规范.docx > 段落 1",
                            ),
                        ),
                    ),
                ),
                refusal_code=None,
                model_calls=1,
                calls=(),
            ),
            rewritten=False,
            stage_count=6,
            calls=(
                ExternalCallAudit(
                    endpoint=(
                        "http://user:"
                        + "secret@192.0.2.10:8000/v1/chat"
                        + "?api_"
                        + "key=secret"
                    ),
                    retry_count=2,
                    elapsed_seconds=0.25,
                ),
            ),
        )
    )

    text = output.getvalue()
    assert "绝不能进入日志" not in text
    assert "secret" not in text
    records = [json.loads(line) for line in text.splitlines()]
    assert records[0]["trace_id"] == "trace-1"
    assert records[0]["stage"] == "rerank"
    assert records[0]["pipeline_fingerprint"] == "sha256:" + "a" * 64
    assert records[0]["serving_fingerprint"] == "sha256:" + "b" * 64
    assert records[1]["chunk_ids"] == ["chunk-1"]
    assert records[2]["endpoint"] == "http://192.0.2.10:8000/v1/chat"
    assert records[2]["retry_count"] == 2
