"""Product API 的活动项目与知识库作用域解析。"""

from __future__ import annotations

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import NotFound


def require_active_knowledge_base(
    runtime: ProductRuntime, knowledge_base_id: str
) -> str:
    """按知识库 ID 解析项目并确认完整作用域仍处于活动状态。

    Args:
        runtime: 当前 Product Runtime。
        knowledge_base_id: 目标知识库 ID。

    Returns:
        已确认处于 active 状态的项目 ID。

    Raises:
        NotFound: 知识库不存在。
        RevisionStateError: 项目已归档或知识库正在删除。

    """
    with runtime.connections.transaction() as connection:
        row = connection.execute(
            "SELECT project_id FROM knowledge_bases "
            "WHERE knowledge_base_id=? AND deleted_at IS NULL",
            (knowledge_base_id,),
        ).fetchone()
    if row is None:
        raise NotFound("知识库不存在。", stage="knowledge_base.read")
    project_id = str(row["project_id"])
    runtime.sdk.require_active_knowledge_base(project_id, knowledge_base_id)
    return project_id


__all__ = ["require_active_knowledge_base"]
