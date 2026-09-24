"""候选链的一次会话指代理解；任何异常都保留原问检索。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from rag_app.application.answering.natural_answer import (
    NaturalCompletion,
    NaturalMessage,
)
from rag_app.core.errors import QueryCancelled, RagError
from rag_app.core.models import ProviderCall, SearchRequest
from rag_app.core.ports import CancellationPort

_NUMBER = re.compile(r"[+-]?\d+(?:[.:/-]\d+)*")
_QUOTED = re.compile(r"[“\"'‘]([^”\"'’]{1,160})[”\"'’]")
_NEGATION = re.compile(r"不得|禁止|不能|不可|不允许|尚未|没有|不是|\bnot\b")
_MAX_REWRITE_CHARS = 512
_MAX_RESPONSE_CHARS = 2048
_SYSTEM = (
    "仅根据最近会话消解本次问题中的指代，输出一个完整检索问句。"
    "本次问题明确的对象、来源、否定、数值和时间范围必须保留；"
    "不得把历史回答当资料事实，不得增加新条件。"
    '只输出 JSON：{"query":"完整问句"}。'
)


@dataclass(frozen=True, slots=True)
class QueryUnderstanding:
    """改写只扩展召回；原问始终由调用方保留。"""

    rewrite: str | None
    reason_code: str
    provider_calls: tuple[ProviderCall, ...] = ()
    model: str | None = None


def understand_query(  # noqa: PLR0911
    request: SearchRequest,
    model: object,
    cancellation: CancellationPort,
) -> QueryUnderstanding:
    """从有限测试会话生成至多一个附加检索问句。"""
    if not request.conversation_context:
        return QueryUnderstanding(None, "NO_HISTORY")
    method = getattr(model, "complete_query_understanding", None)
    if not callable(method):
        return QueryUnderstanding(None, "REWRITE_MODEL_UNAVAILABLE")
    complete = cast(Callable[..., NaturalCompletion], method)
    history = "\n".join(request.conversation_context[-2:])[:1000]
    messages = (
        NaturalMessage(role="system", content=_SYSTEM),
        NaturalMessage(
            role="user",
            content=f"最近会话（仅供理解）：\n{history}\n\n本次问题：{request.text}",
        ),
    )
    try:
        completion = complete(messages, cancellation=cancellation)
    except QueryCancelled:
        raise
    except RagError as error:
        return QueryUnderstanding(
            None,
            error.code,
            tuple(getattr(error, "provider_calls", ())),
        )
    except (ValueError, TypeError):
        return QueryUnderstanding(None, "REWRITE_CALL_INVALID")
    rewrite = _parse_query(completion.text)
    if rewrite is None:
        return QueryUnderstanding(
            None,
            "REWRITE_FORMAT_INVALID",
            completion.provider_calls,
            completion.model,
        )
    if rewrite == request.text.strip():
        return QueryUnderstanding(
            None,
            "REWRITE_SAME_AS_ORIGINAL",
            completion.provider_calls,
            completion.model,
        )
    if not _preserves_explicit_constraints(request.text, rewrite):
        return QueryUnderstanding(
            None,
            "REWRITE_CONSTRAINT_CHANGED",
            completion.provider_calls,
            completion.model,
        )
    return QueryUnderstanding(
        rewrite, "REWRITE_ACCEPTED", completion.provider_calls, completion.model
    )


def _parse_query(raw: str) -> str | None:  # noqa: PLR0911
    content = raw.strip()
    if len(content) > _MAX_RESPONSE_CHARS:
        return None
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    query = parsed.get("query")
    if not isinstance(query, str):
        return None
    query = query.strip()
    if not query or len(query) > _MAX_REWRITE_CHARS or "\n" in query:
        return None
    return query


def _preserves_explicit_constraints(original: str, rewrite: str) -> bool:
    """仅核对可直接观察的硬约束，不引入业务同义词表。"""
    return all(
        value in rewrite
        for pattern in (_NUMBER, _QUOTED, _NEGATION)
        for value in pattern.findall(original)
    )


__all__ = ["QueryUnderstanding", "understand_query"]
