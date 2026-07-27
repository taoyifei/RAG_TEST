"""受控外部模型 HTTP 客户端。"""

from rag_app.clients.resilience import (
    ExternalRequestRejectedError,
    ExternalServiceUnavailableError,
    HttpJsonResponse,
    ResiliencePolicy,
    ResilientHttpPool,
)

__all__ = [
    "ExternalRequestRejectedError",
    "ExternalServiceUnavailableError",
    "HttpJsonResponse",
    "ResiliencePolicy",
    "ResilientHttpPool",
]
