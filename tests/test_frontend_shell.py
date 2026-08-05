from pathlib import Path

_ROOT = Path(__file__).parents[1]


def test_frontend_places_answer_and_trace_before_stage_details() -> None:
    html = (_ROOT / "frontend/index.html").read_text(encoding="utf-8")

    assert "<title>RAGv1</title>" in html
    assert "<h1>RAGv1</h1>" in html
    assert "DOCX RAG 验收壳" not in html
    assert "只发布通过引用校验的回答" not in html
    assert 'id="result"' in html
    assert 'tabindex="-1"' in html
    assert html.index('id="result"') < html.index('id="stages"')
    assert 'id="answer"' in html
    assert 'id="trace-id"' in html
    assert 'id="view-trace"' in html
    assert 'target="rag-trace"' in html


def test_frontend_focuses_result_and_explains_insufficient_evidence() -> None:
    javascript = (_ROOT / "frontend/app.js").read_text(encoding="utf-8")

    assert 'document.querySelector("#result")' in javascript
    assert "scrollIntoView" in javascript
    assert "prefers-reduced-motion: reduce" in javascript
    assert "EVIDENCE_INSUFFICIENT" in javascript
    assert "知识库中暂未找到能够支持该问题的资料" in javascript
    assert 'document.querySelector("#view-trace")' in javascript
    assert '"/debug/?trace_id="' in javascript


def test_trace_page_opens_prefilled_trace_detail() -> None:
    html = (_ROOT / "frontend/debug.html").read_text(encoding="utf-8")
    javascript = (_ROOT / "frontend/debug.js").read_text(encoding="utf-8")

    assert "RAGv1 Trace" in html
    assert 'id="detail-section"' in html
    assert 'tabindex="-1"' in html
    assert "URLSearchParams(window.location.search)" in javascript
    assert 'get("trace_id")' in javascript
    assert "loadDetail(traceId)" in javascript
    assert "请先填写管理令牌" in javascript
    assert "sessionStorage.getItem" in javascript
    assert "sessionStorage.setItem" in javascript
    assert 'id="select-page"' in html
    assert 'id="export-selected"' in html
    assert "/api/admin/traces/export" in javascript
    assert "downloadResponse" in javascript
    assert "全选当前页" in html
