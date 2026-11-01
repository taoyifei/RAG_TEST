"""会话、SQLite 与权限校验共用的稳定错误。"""

# ruff: noqa: N818, PLR0913

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from wanshitong_gateway.compat.core.models.common import (
    JsonObject,
    freeze_json_object,
)

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


class PolicyDenied(RagError):
    """数据出网或项目策略拒绝操作。"""

    default_code = "POLICY_DENIED"


class ProviderUnavailable(RagError):
    """Provider 在有限重试后仍不可用。"""

    default_code = "PROVIDER_UNAVAILABLE"
    default_retryable = True


class ValidationFailed(RagError):
    """跨字段或跨组件验证失败。"""

    default_code = "VALIDATION_FAILED"
