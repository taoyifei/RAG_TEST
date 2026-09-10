"""本地问答正文与安全 Trace 生命周期的独立端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.errors import RagError
from rag_app.core.models import KnowledgeBaseScope
from rag_app.core.models.provider import ProviderCall
from rag_app.core.models.search import RetrievalDiagnostics, SearchAnswerResult


class QueryHistoryPort(Protocol):
    """允许宿主选择正文保存，不把正文放入安全事件端口。"""

    def start(  # noqa: PLR0913
        self,
        trace_id: str,
        scope: KnowledgeBaseScope,
        question: str,
        *,
        owner_id: str,
        save_body: bool,
        conversation_context_digest: str | None = None,
    ) -> None:
        """在读取索引和调用模型前同步创建 STARTED 记录。

        Args:
            trace_id: 当前请求标识。
            scope: 已鉴权的知识库范围。
            question: 当前用户问题。
            owner_id: 当前会话或 Token 主体。
            save_body: 是否允许保存加密正文。
            conversation_context_digest: 可选上下文规范摘要，不含正文。

        Returns:
            写入成功时无返回值。

        """
        ...

    def finish(
        self,
        trace_id: str,
        *,
        result: SearchAnswerResult | None,
        error: RagError | None,
        cancelled: bool,
        cancelled_calls: tuple[ProviderCall, ...] = (),
    ) -> None:
        """写入实际终态和受保护正文。

        Args:
            trace_id: 已创建的请求标识。
            result: 实际查询结果。
            error: 实际安全错误。
            cancelled: 是否已被取消。
            cancelled_calls: 取消前已经发生并结算的脱敏 Provider 调用。

        Returns:
            持久化结束时无返回值。

        """
        ...

    def diagnostics(self, trace_id: str) -> RetrievalDiagnostics:
        """从持久记录读取安全诊断。

        Args:
            trace_id: 待读取的请求标识。

        Returns:
            该请求保存的安全诊断。

        """
        ...
