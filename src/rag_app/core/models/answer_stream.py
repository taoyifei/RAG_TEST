"""默认 Product 回答流的类型化、安全事件。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt

from rag_app.core.models.common import (
    FrozenModel,
    JsonObject,
    freeze_json_object,
)
from rag_app.core.models.retrieval import AnswerClaim
from rag_app.core.models.search import SearchAnswerResult

ANSWER_STREAM_PROTOCOL = "rag-answer-sse-v1"


class AnswerStreamEvent(FrozenModel):
    """所有流事件共用的协议、顺序和请求范围。"""

    protocol: Literal["rag-answer-sse-v1"] = "rag-answer-sse-v1"
    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")
    sequence: StrictInt = Field(ge=0)
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")


class AnswerStreamMetaEvent(AnswerStreamEvent):
    """开流后首先发送、且不含用户正文的协议元数据。"""

    type: Literal["meta"] = "meta"
    delivery: Literal["incremental_or_final_only"] = "incremental_or_final_only"


class AnswerStreamStageEvent(AnswerStreamEvent):
    """不含正文的共享 Application 执行阶段。"""

    type: Literal["stage"] = "stage"
    stage: Literal[
        "accepted",
        "snapshot",
        "retrieval",
        "generation",
        "validation",
    ]
    attributes: JsonObject = ()

    @classmethod
    def create(  # noqa: PLR0913
        cls,
        *,
        trace_id: str,
        sequence: int,
        project_id: str,
        knowledge_base_id: str,
        stage: Literal[
            "accepted",
            "snapshot",
            "retrieval",
            "generation",
            "validation",
        ],
        attributes: object = (),
    ) -> AnswerStreamStageEvent:
        """冻结调用方提供的安全阶段属性。

        Args:
            trace_id: 当前请求的关联 ID。
            sequence: 当前事件的单调序号。
            project_id: 已鉴权项目范围。
            knowledge_base_id: 已鉴权知识库范围。
            stage: 共享查询链阶段。
            attributes: 不含正文的有限阶段属性。

        Returns:
            已验证且不可变的阶段事件。

        """
        return cls(
            trace_id=trace_id,
            sequence=sequence,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            stage=stage,
            attributes=freeze_json_object(attributes),
        )


class AnswerStreamClaimEvent(AnswerStreamEvent):
    """已经完成来源规则校验、但仍由 final 收束的完整 claim。"""

    type: Literal["claim"] = "claim"
    claim_index: StrictInt = Field(ge=0)
    claim: AnswerClaim
    active_index_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    provisional: Literal[True] = True


class AnswerStreamFinalEvent(AnswerStreamEvent):
    """本流唯一权威且唯一允许写成功历史/缓存的终态。"""

    type: Literal["final"] = "final"
    result: SearchAnswerResult


class AnswerStreamErrorEvent(AnswerStreamEvent):
    """响应头发送后的安全错误终态。"""

    type: Literal["error"] = "error"
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=500)
    stage: str = Field(min_length=1, max_length=120)
    retryable: bool = False
    partial: bool = False


class AnswerStreamCancelledEvent(AnswerStreamEvent):
    """显式停止时的取消终态；不声称不可中断上游已经停止。"""

    type: Literal["cancelled"] = "cancelled"
    cancel_requested: Literal[True] = True
    upstream_close_attempted: bool
    upstream_stopped: Literal["confirmed", "unknown"]


AnswerStreamPublicEvent = (
    AnswerStreamMetaEvent
    | AnswerStreamStageEvent
    | AnswerStreamClaimEvent
    | AnswerStreamFinalEvent
    | AnswerStreamErrorEvent
    | AnswerStreamCancelledEvent
)


__all__ = [
    "ANSWER_STREAM_PROTOCOL",
    "AnswerStreamCancelledEvent",
    "AnswerStreamClaimEvent",
    "AnswerStreamErrorEvent",
    "AnswerStreamFinalEvent",
    "AnswerStreamMetaEvent",
    "AnswerStreamPublicEvent",
    "AnswerStreamStageEvent",
]
