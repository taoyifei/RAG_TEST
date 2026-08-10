from pathlib import Path

_ROOT = Path(__file__).parents[1]


def test_debug_admin_token_is_memory_only_by_default() -> None:
    """Trace 明文启用后，Admin Token 不得默认进入浏览器存储。"""
    javascript = (_ROOT / "frontend/debug.js").read_text(encoding="utf-8")

    assert "sessionStorage" not in javascript
    assert "localStorage" not in javascript
    assert "document.cookie" not in javascript
    assert "console." not in javascript
    assert "tokenNode.value" in javascript
