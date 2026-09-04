"""P10 React 控制台源码级安全与交互回归。"""

from pathlib import Path

_ROOT = Path(__file__).parents[1]


def test_frontend_has_one_react_entry_and_no_marketing_slogan() -> None:
    html = (_ROOT / "frontend/index.html").read_text(encoding="utf-8")
    app = (_ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")

    assert 'id="root"' in html
    assert "/src/main.tsx" in html
    assert "从文档到证据" not in app
    assert "当前工作范围" in app
    assert "上传文档" in app


def test_frontend_handles_final_error_and_abort_signals() -> None:
    client = (_ROOT / "frontend/src/api/client.ts").read_text(
        encoding="utf-8"
    )
    app = (_ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")

    assert 'event === "final"' in client
    assert 'event === "error"' in client
    assert "AbortSignal" in client
    assert "SSE 响应缺少 final 事件" in client
    assert "流式回答已中断，请重新提交查询。" in app


def test_evidence_drawer_has_focus_and_escape_handling() -> None:
    ui = (_ROOT / "frontend/src/components/ui.tsx").read_text(
        encoding="utf-8"
    )

    assert 'event.key === "Escape"' in ui
    assert 'event.key !== "Tab"' in ui
    assert 'role="dialog"' in ui
    assert "aria-modal" in ui
