"""湾事通固定 Scope 启动错误。"""

from __future__ import annotations

from rag_app.core.errors import ConfigurationError


class ScopeBindingError(ConfigurationError):
    """持久绑定缺失完整性或与 Universal 对象不一致。"""

    default_code = "WANSHITONG_SCOPE_INVALID"


class AdminFacadeError(Exception):
    """湾事通管理员 Facade 的稳定、安全 HTTP 错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        stage: str = "wanshitong.admin",
    ) -> None:
        """保存可公开错误字段。

        Args:
            code: 供前端稳定判断的错误码。
            message: 可直接展示的中文安全提示。
            status_code: HTTP 状态码。
            stage: 不含动态输入的稳定阶段名。

        """
        self.code = code
        self.message = message
        self.status_code = status_code
        self.stage = stage
        super().__init__(f"{code}: {message}")


__all__ = ["AdminFacadeError", "ScopeBindingError"]
