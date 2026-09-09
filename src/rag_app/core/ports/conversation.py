"""Product 多轮上下文的有界应用端口。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from rag_app.core.models import KnowledgeBaseScope
from rag_app.core.models.search import SearchAnswerResult


class ConversationPort(Protocol):
    """让 SDK 复用查询链而不依赖具体会话数据库。"""

    def lease(
        self,
        scope: KnowledgeBaseScope,
        conversation_id: str,
        *,
        owner_id: str,
    ) -> AbstractContextManager[None]:
        """串行化同一 owner、Project、KB 与会话的查询。

        Args:
            scope: 已鉴权的 Project 与知识库范围。
            conversation_id: 有界会话 ID。
            owner_id: 当前鉴权主体的非秘密身份。

        Returns:
            覆盖上下文读取、查询和提交的独占上下文管理器。

        """
        ...

    def context(
        self,
        scope: KnowledgeBaseScope,
        conversation_id: str,
        *,
        owner_id: str,
    ) -> tuple[str, ...]:
        """返回已重新核验来源的有限历史上下文。

        Args:
            scope: 已鉴权的 Project 与知识库范围。
            conversation_id: 有界会话 ID。
            owner_id: 当前鉴权主体的非秘密身份。

        Returns:
            时间顺序排列的有限上下文轮次。

        """
        ...

    def commit(
        self,
        scope: KnowledgeBaseScope,
        conversation_id: str,
        question: str,
        result: SearchAnswerResult,
        *,
        owner_id: str,
    ) -> bool:
        """只在唯一成功 final 后幂等提交当前轮。

        Args:
            scope: 已鉴权的 Project 与知识库范围。
            conversation_id: 有界会话 ID。
            question: 当前用户问题。
            result: 已交付唯一 final 的查询结果。
            owner_id: 当前鉴权主体的非秘密身份。

        Returns:
            新增轮次时为 True，否则为 False。

        """
        ...


__all__ = ["ConversationPort"]
