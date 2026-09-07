"""Secret 扫描器脱敏输出回归。"""

from scripts.secret_scan import scan_bytes


def test_secret_scan_detects_shapes_without_returning_values() -> None:
    provider_value = b"jina_" + b"a" * 32

    findings = scan_bytes(provider_value, location="fixture")

    assert [(item.location, item.rule) for item in findings] == [
        ("fixture", "jina-api-key")
    ]
    assert provider_value.decode() not in repr(findings)


def test_secret_scan_accepts_safe_configuration() -> None:
    assert scan_bytes(b"JINA_API_KEY=${JINA_API_KEY}", location="safe") == ()
