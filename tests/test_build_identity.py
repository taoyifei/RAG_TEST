"""安装包 revision 与 OCI 期望身份测试。"""

from pathlib import Path

from rag_app.cli import build_info


def test_build_info_reports_mismatch_without_paths_or_secrets() -> None:
    report = build_info(
        installed_revision="a" * 40,
        expected_revision="b" * 40,
    )

    assert report.installed_revision == "a" * 40
    assert report.expected_revision == "b" * 40
    assert report.matches is False
    assert set(report.__dataclass_fields__) == {
        "installed_revision",
        "expected_revision",
        "matches",
    }


def test_dockerfile_compares_installed_revision_after_pip_install() -> None:
    dockerfile = (
        Path(__file__).parents[1] / "Dockerfile"
    ).read_text(encoding="utf-8")

    install = dockerfile.index("python -m pip install")
    import_revision = dockerfile.index(
        "from rag_app._build_revision import SOURCE_REVISION"
    )
    assert install < import_revision
    assert "SOURCE_REVISION == sys.argv[1]" in dockerfile
    assert '"${VCS_REF}"' in dockerfile[import_revision:]
