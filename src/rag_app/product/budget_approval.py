"""以既有管理员会话批准追加预算，不把普通配置当作授权。"""

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_revision import (
    BudgetAuthorizationRevision,
)
from rag_app.product.auth import AuthStore


def approve_budget_revision(
    ledger: ProviderBudgetLedger,
    revision: BudgetAuthorizationRevision,
    *,
    auth: AuthStore,
    session_token: str,
    csrf_token: str,
) -> None:
    """复用产品管理员会话和 CSRF，受限 API Token 不能调用扩额。

    Args:
        ledger: 当前产品已绑定的持久账本。
        revision: 由实际审批人提供的 APPROVED 修订。
        auth: 当前产品的管理员认证存储。
        session_token: 既有管理员 Session，不接受 Bearer API Token。
        csrf_token: 当前管理员会话的非空 CSRF。

    Returns:
        无返回值；审批和会话身份在账本同事务追加审计。

    """
    if not csrf_token:
        raise BudgetBlockedError("BUDGET_ADMIN_CSRF_REQUIRED")
    session_id = auth.validate_session(session_token, csrf_token=csrf_token)
    ledger.apply_revision(revision, admin_session_id=session_id)
