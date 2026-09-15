"""湾事通固定 Scope 启动错误。"""

from __future__ import annotations

from rag_app.core.errors import ConfigurationError


class ScopeBindingError(ConfigurationError):
    """持久绑定缺失完整性或与 Universal 对象不一致。"""

    default_code = "WANSHITONG_SCOPE_INVALID"


class InternalModelConfigurationError(ConfigurationError):
    """内网模型引导状态损坏或与冻结配置不一致。"""

    default_code = "WANSHITONG_MODEL_CONFIGURATION_INVALID"


__all__ = ["InternalModelConfigurationError", "ScopeBindingError"]
