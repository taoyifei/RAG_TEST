"""稳定、可脱敏并可机器处理的 Core 错误。"""

# 公开错误名由跨阶段 schema 固定，不能追加 Error 后缀。
# ruff: noqa: N818, PLR0913

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from rag_app.core.models.common import JsonObject, freeze_json_object
from rag_app.core.models.provider import ProviderCall

_FORBIDDEN_DETAIL_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "document_text",
        "file_path",
        "http_body",
        "raw_response",
        "response_body",
        "secret",
        "sql",
    }
)
_KEY_VALUE_ITEM_LENGTH = 2


class RagError(Exception):
    """所有公共 RAG 错误的安全基类。"""

    default_code: ClassVar[str] = "RAG_ERROR"
    default_retryable: ClassVar[bool] = False

    def __init__(
        self,
        safe_message: str,
        *,
        stage: str,
        code: str | None = None,
        retryable: bool | None = None,
        trace_id: str | None = None,
        details: object = (),
    ) -> None:
        """构造只包含安全说明和结构细节的错误。

        Args:
            safe_message: 可安全展示的人类说明。
            stage: 失败阶段的稳定名称。
            code: 可选覆盖的机器错误码。
            retryable: 可选覆盖的可重试标志。
            trace_id: 可选安全 trace ID。
            details: 不含正文、路径、SQL、响应体或 secret 的 JSON object。

        Returns:
            无返回值；初始化异常实例。

        """
        self.code = code or self.default_code
        self.safe_message = safe_message
        self.retryable = (
            self.default_retryable if retryable is None else retryable
        )
        self.stage = stage
        self.trace_id = trace_id
        self.provider_call: ProviderCall | None = None
        self.provider_calls: tuple[ProviderCall, ...] = ()
        frozen_details = freeze_json_object(details)
        if _contains_forbidden_details(frozen_details):
            raise ValueError("错误 details 包含禁止的敏感字段。")
        self.details: JsonObject = frozen_details
        super().__init__(f"{self.code}: {self.safe_message}")


def _contains_forbidden_details(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            key.casefold() in _FORBIDDEN_DETAIL_KEYS
            or _contains_forbidden_details(item)
            for key, item in value.items()
            if isinstance(key, str)
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if all(
            isinstance(item, (tuple, list))
            and len(item) == _KEY_VALUE_ITEM_LENGTH
            and isinstance(item[0], str)
            for item in value
        ):
            return any(
                str(item[0]).casefold() in _FORBIDDEN_DETAIL_KEYS
                or _contains_forbidden_details(item[1])
                for item in value
            )
        return any(_contains_forbidden_details(item) for item in value)
    return False


class ConfigurationError(RagError):
    """配置无效或不完整。"""

    default_code = "CONFIGURATION_ERROR"


class ComponentNotRegistered(RagError):
    """Profile 引用了未显式注册的组件。"""

    default_code = "COMPONENT_NOT_REGISTERED"


class CapabilityMismatch(RagError):
    """已注册组件能力无法组成安全链路。"""

    default_code = "CAPABILITY_MISMATCH"


class CapabilityUnavailable(RagError):
    """应用用例尚未迁移到当前引擎。"""

    default_code = "CAPABILITY_UNAVAILABLE"


class PolicyDenied(RagError):
    """数据出网或项目策略拒绝操作。"""

    default_code = "POLICY_DENIED"


class InvalidDocument(RagError):
    """文档输入不满足安全或格式合同。"""

    default_code = "INVALID_DOCUMENT"


class UnsupportedDocumentFeature(RagError):
    """文档包含尚未接受语义的功能。"""

    default_code = "UNSUPPORTED_DOCUMENT_FEATURE"


class ProviderUnavailable(RagError):
    """Provider 在有限重试后仍不可用。"""

    default_code = "PROVIDER_UNAVAILABLE"
    default_retryable = True


class ProviderAuthenticationError(RagError):
    """Provider 鉴权或模型身份无效。"""

    default_code = "PROVIDER_AUTHENTICATION_ERROR"


class ProviderRateLimited(RagError):
    """Provider 拒绝当前速率。"""

    default_code = "PROVIDER_RATE_LIMITED"
    default_retryable = True


class ProviderInvalidResponse(RagError):
    """Provider 响应违反数量、维度或数值合同。"""

    default_code = "PROVIDER_INVALID_RESPONSE"


class ProviderInputTooLarge(RagError):
    """调用方提供的输入超过本地限制。"""

    default_code = "PROVIDER_INPUT_TOO_LARGE"


class DenseUnavailable(RagError):
    """两个 Dense slot 均不可安全使用。"""

    default_code = "DENSE_UNAVAILABLE"
    default_retryable = True


class IndexCompatibilityError(RagError):
    """查询 slot、vector name 或 revision 不匹配。"""

    default_code = "INDEX_COMPATIBILITY_ERROR"


class IndexNotReady(RagError):
    """知识库没有可供普通查询读取的 Active Revision。"""

    default_code = "INDEX_NOT_READY"


class IndexCorrupt(RagError):
    """索引通道身份无法与 canonical store 一致回读。"""

    default_code = "INDEX_CORRUPT"


class RevisionStateError(RagError):
    """索引 revision 状态不允许当前转换。"""

    default_code = "REVISION_STATE_ERROR"


class NotFound(RagError):
    """目标逻辑资源不存在。"""

    default_code = "NOT_FOUND"


class Conflict(RagError):
    """目标资源或注册名已经存在。"""

    default_code = "CONFLICT"


class ValidationFailed(RagError):
    """跨字段或跨组件验证失败。"""

    default_code = "VALIDATION_FAILED"


__all__ = [
    "CapabilityMismatch",
    "CapabilityUnavailable",
    "ComponentNotRegistered",
    "ConfigurationError",
    "Conflict",
    "DenseUnavailable",
    "IndexCompatibilityError",
    "IndexCorrupt",
    "IndexNotReady",
    "InvalidDocument",
    "NotFound",
    "PolicyDenied",
    "ProviderAuthenticationError",
    "ProviderInputTooLarge",
    "ProviderInvalidResponse",
    "ProviderRateLimited",
    "ProviderUnavailable",
    "RagError",
    "RevisionStateError",
    "UnsupportedDocumentFeature",
    "ValidationFailed",
]
