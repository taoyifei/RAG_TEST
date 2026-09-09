"""P10 React 控制台源码级安全与交互回归。"""

from pathlib import Path

_ROOT = Path(__file__).parents[1]


def test_frontend_has_one_react_entry_and_no_marketing_slogan() -> None:
    html = (_ROOT / "frontend/index.html").read_text(encoding="utf-8")
    app = (_ROOT / "frontend/src/app/AppShell.tsx").read_text(encoding="utf-8")
    dashboard = (_ROOT / "frontend/src/pages/Dashboard.tsx").read_text(
        encoding="utf-8"
    )

    assert 'id="root"' in html
    assert "/src/main.tsx" in html
    assert "从文档到证据" not in app + dashboard
    assert "当前工作范围" in app
    assert "上传文档" in dashboard


def test_frontend_handles_versioned_stream_final_error_and_abort() -> None:
    client = (_ROOT / "frontend/src/api/client.ts").read_text(encoding="utf-8")
    app = (_ROOT / "frontend/src/pages/QueryPage.tsx").read_text(
        encoding="utf-8"
    )

    assert 'eventName === "claim"' in client
    assert 'eventName === "final"' in client
    assert 'eventName === "error"' in client
    assert 'new TextDecoder("utf-8", { fatal: true })' in client
    assert "await reader.cancel()" in client
    assert "AbortSignal" in client
    assert "SSE 响应缺少合法终态" in client
    assert "暂存内容不是最终答案" in app
    assert "await api.answerStream(" in app
    assert "await api.answer(" not in app


def test_evidence_drawer_has_focus_and_escape_handling() -> None:
    ui = (_ROOT / "frontend/src/components/ui.tsx").read_text(encoding="utf-8")

    assert 'event.key === "Escape"' in ui
    assert 'event.key !== "Tab"' in ui
    assert 'role="dialog"' in ui
    assert "aria-modal" in ui
