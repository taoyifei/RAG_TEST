"""防止 SSO 一次性票据进入 Uvicorn 访问日志。"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from uvicorn.config import LOGGING_CONFIG

_SSO_QUERY_PATH_SUFFIXES = ("/sso/entry", "/sso/callback")
_ACCESS_TARGET_ARGUMENT_INDEX = 2


class SsoQueryRedactionFilter(logging.Filter):
    """只从 SSO entry/callback 访问记录中移除完整查询串。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """在 Uvicorn formatter 读取参数前替换请求 target。"""
        arguments = record.args
        if (
            not isinstance(arguments, tuple)
            or len(arguments) <= _ACCESS_TARGET_ARGUMENT_INDEX
        ):
            return True
        target = arguments[_ACCESS_TARGET_ARGUMENT_INDEX]
        if not isinstance(target, str) or "?" not in target:
            return True
        path = target.split("?", 1)[0]
        if not path.endswith(_SSO_QUERY_PATH_SUFFIXES):
            return True
        sanitized = list(arguments)
        sanitized[_ACCESS_TARGET_ARGUMENT_INDEX] = path
        record.args = tuple(sanitized)
        return True


def sso_safe_access_log_config() -> dict[str, Any]:
    """返回保留普通访问日志、但过滤 SSO query 的 Uvicorn 配置。"""
    config = deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["sso_query_redaction"] = {
        "()": SsoQueryRedactionFilter
    }
    access_handler = config["handlers"]["access"]
    access_handler["filters"] = ["sso_query_redaction"]
    return config


__all__ = ["SsoQueryRedactionFilter", "sso_safe_access_log_config"]
