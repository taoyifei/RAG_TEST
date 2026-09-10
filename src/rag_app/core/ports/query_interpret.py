"""受控、单次结构化问题解释的纯数据端口。"""

from dataclasses import dataclass
from typing import Protocol

from rag_app.core.models import (
    ProviderCall,
    QueryAnalysis,
    QuerySemantics,
    SearchRequest,
)


@dataclass(frozen=True)
class InterpretOutcome:
    """保留原问并返回经服务端校验的共享语义。"""

    standalone_query: str | None = None
    semantics: QuerySemantics | None = None
    calls: tuple[ProviderCall, ...] = ()
    reason_code: str = "INTERPRET_NOT_NEEDED"
    attempted: bool = False


class QueryInterpretPort(Protocol):
    """规则低置信时可调用一次的结构化解释端口。"""

    def interpret(
        self, request: SearchRequest, analysis: QueryAnalysis
    ) -> InterpretOutcome:
        """解释问题形状，但不能回答问题或改变原始硬约束。

        Args:
            request: 原问题、鉴权范围和有界会话上下文。
            analysis: 原问题的权威确定性分析。

        Returns:
            可选独立问题、共享语义和实际 Provider 调用审计。

        """
        ...


__all__ = ["InterpretOutcome", "QueryInterpretPort"]
