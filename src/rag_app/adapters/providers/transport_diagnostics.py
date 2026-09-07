"""有限白名单传输诊断；绝不序列化异常正文或请求对象。"""

from __future__ import annotations

import errno
import math
import re
import socket
import ssl
from collections.abc import Mapping
from typing import Any

import httpx

_MAX_SAFE_ATTEMPTS = 1000
_ATTEMPT_ID = re.compile(r"attempt-[a-f0-9]{32}\Z")
_ERROR_TYPES = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.CloseError,
    httpx.RemoteProtocolError,
    httpx.LocalProtocolError,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
)
_RETRYABLE = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)
_CAUSE_TYPES = (
    ssl.SSLCertVerificationError,
    ssl.SSLError,
    socket.gaierror,
    ConnectionRefusedError,
    ConnectionResetError,
    BrokenPipeError,
    TimeoutError,
    OSError,
)
_CERTIFICATE_REASONS = {
    9: "CERTIFICATE_NOT_YET_VALID",
    10: "CERTIFICATE_EXPIRED",
    18: "SELF_SIGNED_CERTIFICATE",
    19: "SELF_SIGNED_CERTIFICATE_IN_CHAIN",
    20: "UNABLE_TO_GET_LOCAL_ISSUER",
    21: "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
    62: "HOSTNAME_MISMATCH",
}


def transport_diagnostics(
    error: httpx.HTTPError | None,
    *,
    elapsed_ms: int,
    extensions: Mapping[str, object],
) -> dict[str, Any]:
    """提取单次传输的结构化安全事实，未知根因保持 UNKNOWN。

    Args:
        error: 当前尝试的真实异常，成功时为空。
        elapsed_ms: 只计当前尝试的单调时钟耗时。
        extensions: httpx 的实际 timeout 和仓库受控尝试关联字段。

    Returns:
        不含异常消息、URL、请求头或正文的有限诊断对象。

    """
    result: dict[str, Any] = {
        "transport_error_type": "NONE" if error is None else "UNKNOWN",
        "cause_type": "UNKNOWN",
        "errno": "UNKNOWN",
        "certificate_reason": "UNKNOWN",
        "attempt_elapsed_ms": max(0, elapsed_ms),
        "retry_index": _safe_count(extensions.get("rag_provider_retry_index")),
        "max_attempts": _safe_count(
            extensions.get("rag_provider_max_attempts")
        ),
        "locally_blocked": extensions.get("rag_locally_blocked") is True,
        "timeout_seconds": _timeouts(extensions.get("timeout")),
    }
    attempt_id = extensions.get("rag_budget_attempt_id")
    result["attempt_id"] = (
        attempt_id
        if isinstance(attempt_id, str) and _ATTEMPT_ID.fullmatch(attempt_id)
        else "UNKNOWN"
    )
    for error_type in _ERROR_TYPES:
        if type(error) is error_type:
            result["transport_error_type"] = error_type.__name__
            break
    cause: BaseException | None = error
    seen: set[int] = set()
    for _ in range(6):
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        if type(cause) in _CAUSE_TYPES:
            result["cause_type"] = type(cause).__name__
            code = getattr(cause, "errno", None)
            if type(code) is int and (
                code in errno.errorcode
                or (
                    isinstance(cause, socket.gaierror)
                    and code
                    in {socket.EAI_AGAIN, socket.EAI_NONAME, socket.EAI_FAIL}
                )
            ):
                result["errno"] = code
            if isinstance(cause, ssl.SSLCertVerificationError):
                verify_code = getattr(cause, "verify_code", None)
                result["certificate_reason"] = (
                    _CERTIFICATE_REASONS.get(verify_code, "UNKNOWN")
                    if type(verify_code) is int
                    else "UNKNOWN"
                )
                break
        cause = cause.__cause__ or cause.__context__
    return result


def retryable_transport(
    error: httpx.TransportError, diagnostics: Mapping[str, object]
) -> bool:
    """仅重试已知瞬时类别，已知证书失败不盲目重试。

    Args:
        error: 当前真实传输异常。
        diagnostics: 已白名单归一化的诊断。

    Returns:
        是否允许既有的有界重试策略继续。

    """
    return isinstance(error, _RETRYABLE) and diagnostics.get(
        "cause_type"
    ) not in {
        "SSLCertVerificationError",
        "SSLError",
    }


def _safe_count(value: object) -> int | str:
    return (
        value
        if type(value) is int and 0 <= value <= _MAX_SAFE_ATTEMPTS
        else "UNKNOWN"
    )


def _timeouts(value: object) -> dict[str, float | str]:
    values = value if isinstance(value, dict) else {}
    result: dict[str, float | str] = {}
    for key in ("connect", "read", "write", "pool"):
        timeout = values.get(key, "UNKNOWN")
        if timeout is None:
            result[key] = "UNBOUNDED"
        elif (
            type(timeout) in (int, float)
            and math.isfinite(timeout)
            and timeout >= 0
        ):
            result[key] = float(timeout)
        else:
            result[key] = "UNKNOWN"
    return result
