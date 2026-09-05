"""预算授权边界的稳定拒绝原因。"""

from collections.abc import Mapping

from rag_app.core.errors import PolicyDenied


class BudgetBlockedError(PolicyDenied):
    """预算或授权边界在实际 HTTP 发送前拒绝请求。"""

    def __init__(
        self, reason: str, minimum_additional: Mapping[str, int] | None = None
    ) -> None:
        self.reason = reason
        self.minimum_additional = dict(minimum_additional or {})
        super().__init__(
            reason,
            stage="provider.budget",
            code=reason,
            details={"minimum_additional": self.minimum_additional},
        )
