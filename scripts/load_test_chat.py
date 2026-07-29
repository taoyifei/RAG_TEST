"""以 5 并发持续 30 分钟验收已部署的 chat NDJSON 接口。"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import re
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

import httpx

from evaluation.active_state import (
    add_active_state_arguments,
    load_live_active_evidence,
)
from rag_app.active_evidence import (
    ActiveEvidenceManifest,
    ActiveEvidenceRecord,
)

_MAX_ERROR_RATE = 0.01
_MAX_ANSWER_P95_SECONDS = 60.0
_HTTP_OK = 200
_EVIDENCE_ID_PATTERN = re.compile(r"^E[1-9][0-9]*$")


class RequestOutcome(StrEnum):
    """负载请求的互斥终态。"""

    ANSWERED = "answered"
    CORRECT_REFUSAL = "correct_refusal"
    INCORRECT_REFUSAL = "incorrect_refusal"
    UNEXPECTED_ANSWER = "unexpected_answer"
    INVALID_CITATION = "invalid_citation"
    HTTP_ERROR = "http_error"
    PARSE_ERROR = "parse_error"
    PROTOCOL_ERROR = "protocol_error"
    HISTORY_COMPLETED = "history_completed"


@dataclass(frozen=True, slots=True)
class LoadCase:
    """一条不含冻结答案的负载请求契约。"""

    identifier: str
    question: str
    history_questions: tuple[str, ...]
    expected_answerable: bool


@dataclass(frozen=True, slots=True)
class RequestResult:
    """单次请求的非敏感耗时、角色与终态。"""

    elapsed_seconds: float
    outcome: RequestOutcome
    target: bool
    multiturn: bool


@dataclass(frozen=True, slots=True)
class _LoadRuntime:
    """所有并发用户共享的只读负载配置。"""

    url: str
    token: str
    cases: tuple[LoadCase, ...]
    active_manifest: ActiveEvidenceManifest


@dataclass(frozen=True, slots=True)
class _RequestContext:
    """一次 HTTP 请求的分类上下文。"""

    conversation_id: str
    question: str
    expected_answerable: bool | None
    target: bool
    multiturn: bool


def main() -> int:
    """运行固定并发负载并输出分类质量与性能报告。

    Args:
        无参数。

    Returns:
        全部质量和性能门槛通过时返回 0，否则返回 1。

    """
    arguments = _arguments()
    cases = _load_cases(arguments.dataset)
    active_manifest = load_live_active_evidence(arguments)
    runtime = _LoadRuntime(
        url=arguments.url,
        token=arguments.token,
        cases=cases,
        active_manifest=active_manifest,
    )
    deadline = time.monotonic() + arguments.duration_seconds
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=arguments.concurrency
    ) as executor:
        futures = [
            executor.submit(
                _user_loop,
                user_index,
                deadline,
                runtime,
            )
            for user_index in range(arguments.concurrency)
        ]
        results = [
            result for future in futures for result in future.result()
        ]
    report = summarize_results(
        results,
        concurrency=arguments.concurrency,
        duration_seconds=arguments.duration_seconds,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


def summarize_results(
    results: Sequence[RequestResult],
    *,
    concurrency: int,
    duration_seconds: int,
) -> dict[str, object]:
    """汇总互斥终态并执行负载质量门槛。

    Args:
        results: 全部历史预热和目标请求结果。
        concurrency: 实际并发用户数。
        duration_seconds: 计划持续秒数。

    Returns:
        不包含问题、回答、引用原文或令牌的机器可读报告。

    """
    target_results = [result for result in results if result.target]
    counts = Counter(
        result.outcome.value for result in target_results
    )
    transport_outcomes = {
        RequestOutcome.HTTP_ERROR.value,
        RequestOutcome.PARSE_ERROR.value,
        RequestOutcome.PROTOCOL_ERROR.value,
    }
    transport_errors = sum(
        result.outcome.value in transport_outcomes for result in results
    )
    error_rate = transport_errors / len(results) if results else 1.0
    accepted_outcomes = {
        RequestOutcome.ANSWERED,
        RequestOutcome.CORRECT_REFUSAL,
    }
    latencies = [
        result.elapsed_seconds
        for result in target_results
        if result.outcome in accepted_outcomes
    ]
    answer_p95 = _percentile(latencies, 0.95) if latencies else None
    quality_failures = sum(
        counts[outcome.value]
        for outcome in (
            RequestOutcome.INCORRECT_REFUSAL,
            RequestOutcome.UNEXPECTED_ANSWER,
            RequestOutcome.INVALID_CITATION,
        )
    )
    report: dict[str, object] = {
        "concurrency": concurrency,
        "duration_seconds": duration_seconds,
        "requests": len(results),
        "target_requests": len(target_results),
        "history_requests": sum(not result.target for result in results),
        "multiturn_cases": sum(
            result.target and result.multiturn for result in results
        ),
        "answered": counts[RequestOutcome.ANSWERED.value],
        "correct_refusals": counts[
            RequestOutcome.CORRECT_REFUSAL.value
        ],
        "incorrect_refusals": counts[
            RequestOutcome.INCORRECT_REFUSAL.value
        ],
        "unexpected_answers": counts[
            RequestOutcome.UNEXPECTED_ANSWER.value
        ],
        "invalid_citations": counts[
            RequestOutcome.INVALID_CITATION.value
        ],
        "http_errors": sum(
            result.outcome == RequestOutcome.HTTP_ERROR
            for result in results
        ),
        "parse_errors": sum(
            result.outcome == RequestOutcome.PARSE_ERROR
            for result in results
        ),
        "protocol_errors": sum(
            result.outcome == RequestOutcome.PROTOCOL_ERROR
            for result in results
        ),
        "errors": transport_errors,
        "error_rate": error_rate,
        "answer_p95_seconds": answer_p95,
        "passed": bool(
            target_results
            and latencies
            and error_rate < _MAX_ERROR_RATE
            and quality_failures == 0
            and answer_p95 is not None
            and answer_p95 <= _MAX_ANSWER_P95_SECONDS
        ),
    }
    return report


def classify_final(
    final: Mapping[str, object],
    *,
    expected_answerable: bool | None,
    active_evidence_manifest: ActiveEvidenceManifest,
) -> RequestOutcome:
    """校验最终事件并按可回答性分类。

    Args:
        final: NDJSON 中最后一条 final 事件。
        expected_answerable: 目标题的冻结可回答性；历史预热为 `None`。
        active_evidence_manifest: 当前进程现场扫描产生的活动证据。

    Returns:
        回答、正确/错误拒答、无效引用或协议错误之一。

    """
    if final.get("type") != "final":
        return RequestOutcome.PROTOCOL_ERROR
    status = final.get("status")
    if status == "answered":
        if not _citations_are_valid(final, active_evidence_manifest):
            return RequestOutcome.INVALID_CITATION
        return _answered_outcome(expected_answerable)
    if status == "refused":
        return _refused_outcome(expected_answerable)
    return RequestOutcome.PROTOCOL_ERROR


def _answered_outcome(
    expected_answerable: bool | None,
) -> RequestOutcome:
    if expected_answerable is None:
        return RequestOutcome.HISTORY_COMPLETED
    if expected_answerable:
        return RequestOutcome.ANSWERED
    return RequestOutcome.UNEXPECTED_ANSWER


def _refused_outcome(
    expected_answerable: bool | None,
) -> RequestOutcome:
    if expected_answerable is None:
        return RequestOutcome.HISTORY_COMPLETED
    if expected_answerable:
        return RequestOutcome.INCORRECT_REFUSAL
    return RequestOutcome.CORRECT_REFUSAL


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("evaluation/frozen/dataset.json"),
    )
    add_active_state_arguments(parser)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--duration-seconds", type=int, default=1800)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evaluation/results/chat-load.json"),
    )
    return parser.parse_args()


def _load_cases(path: Path) -> tuple[LoadCase, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(raw_cases, list):
        raise ValueError("冻结集缺少 cases。")
    cases = []
    for item in raw_cases:
        if not isinstance(item, dict):
            continue
        if item.get("validation_state", "verified_text") != "verified_text":
            continue
        expected = item.get("expected")
        history = item.get("history_questions", [])
        if (
            not isinstance(item.get("id"), str)
            or not isinstance(item.get("question"), str)
            or not isinstance(expected, dict)
            or not isinstance(expected.get("answerable"), bool)
            or not isinstance(history, list)
            or not all(isinstance(question, str) for question in history)
        ):
            raise ValueError("冻结集含不完整的负载 case。")
        cases.append(
            LoadCase(
                identifier=item["id"],
                question=item["question"],
                history_questions=tuple(history),
                expected_answerable=expected["answerable"],
            )
        )
    if not cases:
        raise ValueError("冻结集没有可压测问题。")
    return tuple(cases)


def _user_loop(
    user_index: int,
    deadline: float,
    runtime: _LoadRuntime,
) -> list[RequestResult]:
    results: list[RequestResult] = []
    request_index = 0
    with httpx.Client(
        timeout=httpx.Timeout(65.0, connect=3.0),
        trust_env=False,
    ) as client:
        while time.monotonic() < deadline:
            case = runtime.cases[
                (user_index + request_index) % len(runtime.cases)
            ]
            conversation_id = (
                f"load-{user_index}-{uuid.uuid4().hex}"
            )
            history_ok = True
            for history_question in case.history_questions:
                history_result = _ask(
                    client,
                    runtime,
                    _RequestContext(
                        conversation_id=conversation_id,
                        question=history_question,
                        expected_answerable=None,
                        target=False,
                        multiturn=True,
                    ),
                )
                results.append(history_result)
                if history_result.outcome != RequestOutcome.HISTORY_COMPLETED:
                    history_ok = False
                    break
            if history_ok:
                results.append(
                    _ask(
                        client,
                        runtime,
                        _RequestContext(
                            conversation_id=conversation_id,
                            question=case.question,
                            expected_answerable=case.expected_answerable,
                            target=True,
                            multiturn=bool(case.history_questions),
                        ),
                    )
                )
            request_index += 1
    return results


def _ask(
    client: httpx.Client,
    runtime: _LoadRuntime,
    context: _RequestContext,
) -> RequestResult:
    started = time.perf_counter()
    try:
        with client.stream(
            "POST",
            f"{runtime.url.rstrip('/')}/api/chat",
            headers={"Authorization": f"Bearer {runtime.token}"},
            json={
                "conversation_id": context.conversation_id,
                "question": context.question,
            },
        ) as response:
            if response.status_code != _HTTP_OK:
                return _request_result(
                    started,
                    RequestOutcome.HTTP_ERROR,
                    context.target,
                    context.multiturn,
                )
            messages = [
                json.loads(line)
                for line in response.iter_lines()
                if line.strip()
            ]
    except httpx.HTTPError:
        return _request_result(
            started,
            RequestOutcome.HTTP_ERROR,
            context.target,
            context.multiturn,
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _request_result(
            started,
            RequestOutcome.PARSE_ERROR,
            context.target,
            context.multiturn,
        )
    finals = [
        message
        for message in messages
        if isinstance(message, dict) and message.get("type") == "final"
    ]
    if not messages or len(finals) != 1 or messages[-1] is not finals[0]:
        outcome = RequestOutcome.PROTOCOL_ERROR
    else:
        outcome = classify_final(
            finals[0],
            expected_answerable=context.expected_answerable,
            active_evidence_manifest=runtime.active_manifest,
        )
    return _request_result(
        started,
        outcome,
        context.target,
        context.multiturn,
    )


def _request_result(
    started: float,
    outcome: RequestOutcome,
    target: bool,
    multiturn: bool,
) -> RequestResult:
    return RequestResult(
        elapsed_seconds=time.perf_counter() - started,
        outcome=outcome,
        target=target,
        multiturn=multiturn,
    )


def _citations_are_valid(
    final: Mapping[str, object],
    manifest: ActiveEvidenceManifest,
) -> bool:
    claims = final.get("claims")
    if not isinstance(claims, list) or not claims:
        return False
    active_records = {
        record.chunk_id: record for record in manifest.records
    }
    evidence_ids: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            return False
        supports = claim.get("supports")
        if not isinstance(supports, list) or not supports:
            return False
        for support in supports:
            if not _support_is_valid(
                support,
                active_records,
                evidence_ids,
            ):
                return False
    return True


def _support_is_valid(
    support: object,
    active_records: Mapping[str, ActiveEvidenceRecord],
    evidence_ids: set[str],
) -> bool:
    fields = _support_string_fields(support)
    if fields is None:
        return False
    evidence_id, chunk_id, locator, quote = fields
    if (
        not _EVIDENCE_ID_PATTERN.fullmatch(evidence_id)
        or evidence_id in evidence_ids
    ):
        return False
    evidence_ids.add(evidence_id)
    record = active_records.get(chunk_id)
    return bool(
        record is not None
        and locator == record.locator
        and quote
        and quote in record.text
    )


def _support_string_fields(
    support: object,
) -> tuple[str, str, str, str] | None:
    if not isinstance(support, dict):
        return None
    values = (
        support.get("evidence_id"),
        support.get("chunk_id"),
        support.get("locator"),
        support.get("quote"),
    )
    if not all(isinstance(value, str) for value in values):
        return None
    return cast(tuple[str, str, str, str], values)


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 3)


if __name__ == "__main__":
    raise SystemExit(main())
