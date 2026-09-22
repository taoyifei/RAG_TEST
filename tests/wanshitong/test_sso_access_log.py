"""SSO 访问日志不得保留 ticket 或 state。"""

from __future__ import annotations

import logging

from rag_app.wanshitong.access_log import SsoQueryRedactionFilter


def _record(target: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.0.1", "GET", target, "1.1", 302),
        exc_info=None,
    )


def test_sso_access_log_removes_sensitive_query() -> None:
    record = _record(
        "/kb/sso/callback?ticket=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&state=secret"
    )

    assert SsoQueryRedactionFilter().filter(record) is True
    rendered = record.getMessage()

    assert "/kb/sso/callback" in rendered
    assert "ticket" not in rendered
    assert "state" not in rendered
    assert "secret" not in rendered


def test_access_log_keeps_non_sso_query() -> None:
    record = _record("/api/v1/jobs?page=2")

    SsoQueryRedactionFilter().filter(record)

    assert "?page=2" in record.getMessage()
