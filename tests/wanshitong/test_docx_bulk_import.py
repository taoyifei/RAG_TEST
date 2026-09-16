"""WB-07R 部署侧 DOCX 批量导入器定向回归。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Protocol

import pytest

_REPOSITORY_ROOT = Path(__file__).parents[2]
_MANIFEST_PATH = _REPOSITORY_ROOT / "DOCX_ONLY_MANIFEST_46.json"
_IMPORTER_PATH = (
    _REPOSITORY_ROOT / "deployment" / "wanshitong" / "import_docx.py"
)


class _PreparedLike(Protocol):
    api_path: str


def _load_importer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "wanshitong_docx_importer", _IMPORTER_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError("无法加载部署侧 DOCX 导入器。")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_docx(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            "<w:document>" + marker + "</w:document>",
        )


def _build_corpus(tmp_path: Path) -> tuple[Path, Path]:
    payload = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    corpus = tmp_path / "corpus"
    for item in payload["documents"]:
        _write_docx(corpus / item["source_relative_path"], item["document_id"])
    manifest = tmp_path / "DOCX_ONLY_MANIFEST_46.json"
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return corpus, manifest


def _token(tmp_path: Path) -> Path:
    target = tmp_path / "admin-bootstrap-token"
    target.write_text("fixture-value", encoding="utf-8")
    target.chmod(0o600)
    return target


class _FakeClient:
    def __init__(
        self, existing: list[dict[str, object]] | None = None
    ) -> None:
        self.existing = [] if existing is None else existing
        self.upload_count = 0
        self.version_count = 0
        self.template_refresh_count = 0
        self.documents: dict[str, dict[str, object]] = {}
        for item in self.existing:
            document_id = item.get("document_id")
            if isinstance(document_id, str):
                self.documents[document_id] = dict(item)

    def login(self, bootstrap_token: str) -> None:
        assert bootstrap_token.startswith("fixture-")

    def list_documents(self) -> list[dict[str, object]]:
        return self.existing

    def upload(self, document: _PreparedLike) -> dict[str, object]:
        self.upload_count += 1
        document_id = f"doc_{self.upload_count:032x}"
        job_id = f"job_{self.upload_count:032x}"
        self.documents[document_id] = {
            "document_id": document_id,
            "source_relative_path": document.api_path,
            "retrievable": True,
        }
        return {
            "document": {
                "document_id": document_id,
                "retrievable": False,
            },
            "job": {"job_id": job_id, "state": "queued"},
        }

    def get_job(self, job_id: str) -> dict[str, object]:
        return {"job_id": job_id, "state": "succeeded"}

    def refresh_template_catalog(
        self, document_id: str, document: _PreparedLike
    ) -> dict[str, object]:
        assert "模板" in document.api_path
        self.template_refresh_count += 1
        return {
            "document": {"document_id": document_id},
            "job": {
                "job_id": f"job_{200 + self.template_refresh_count:032x}",
                "state": "queued",
            },
        }

    def upload_version(
        self,
        document_id: str,
        document: _PreparedLike,
        *,
        failed_job_id: str,
    ) -> dict[str, object]:
        assert failed_job_id.startswith("job_")
        self.version_count += 1
        job_id = f"job_{100 + self.version_count:032x}"
        self.documents[document_id] = {
            "document_id": document_id,
            "source_relative_path": document.api_path,
            "retrievable": True,
        }
        return {
            "document": {
                "document_id": document_id,
                "retrievable": False,
            },
            "job": {"job_id": job_id, "state": "queued"},
        }

    def retry_job(self, job_id: str) -> dict[str, object]:
        raise AssertionError(f"不应重试成功 Job：{job_id}")

    def get_document(self, document_id: str) -> dict[str, object]:
        return self.documents[document_id]


def _arguments(
    tmp_path: Path,
    corpus: Path,
    manifest: Path,
    *,
    resume: bool = False,
    recover_terminal: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        base_url="http://127.0.0.1:8288",
        bootstrap_token_file=_token(tmp_path),
        root=corpus,
        manifest=manifest,
        pilot_only=True,
        resume=resume,
        recover_terminal=recover_terminal,
        refresh_templates=False,
        wait=True,
        report=tmp_path / "import-report.json",
        timeout_seconds=2,
        poll_seconds=0.001,
    )


def test_manifest_uses_literal_disk_path_and_nfkc_api_identity(
    tmp_path: Path,
) -> None:
    importer = _load_importer()
    corpus, manifest = _build_corpus(tmp_path)

    documents = importer.load_and_validate_manifest(manifest, corpus)

    assert len(documents) == 46
    assert sum(item.pilot for item in documents) == 4
    entity_case = next(
        item for item in documents if item.control_id == "DOCX-035"
    )
    assert "&amp;" in entity_case.literal_path
    assert "&amp;" in entity_case.api_path
    assert entity_case.file_path.is_file()


def test_manifest_rejects_missing_or_invalid_docx(tmp_path: Path) -> None:
    importer = _load_importer()
    corpus, manifest = _build_corpus(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    broken = corpus / payload["documents"][0]["source_relative_path"]
    broken.write_bytes(b"not-a-docx")

    with pytest.raises(importer.ImportContractError, match="DOCX"):
        importer.load_and_validate_manifest(manifest, corpus)


def test_pilot_import_waits_until_four_documents_are_retrievable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = _load_importer()
    corpus, manifest = _build_corpus(tmp_path)
    fake = _FakeClient()
    monkeypatch.setattr(
        importer, "WanshitongAdminClient", lambda _base_url: fake
    )

    report = importer.run_import(
        _arguments(tmp_path, corpus, manifest)
    )

    assert report["manifest_count"] == 46
    assert report["selected_count"] == 4
    assert report["registered"] == 4
    assert report["ingestion_succeeded"] == 4
    assert report["retrievable"] == 4
    assert report["failed"] == 0
    assert fake.upload_count == 4
    serialized = (tmp_path / "import-report.json").read_text(encoding="utf-8")
    assert "fixture-value" not in serialized
    assert "<w:document>" not in serialized


def test_resume_skips_already_retrievable_pilot_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = _load_importer()
    corpus, manifest = _build_corpus(tmp_path)
    prepared = importer.load_and_validate_manifest(manifest, corpus)
    existing = [
        {
            "document_id": f"doc_{index:032x}",
            "source_relative_path": item.api_path,
            "retrievable": True,
            "latest_job": {
                "job_id": f"job_{index:032x}",
                "state": "succeeded",
            },
        }
        for index, item in enumerate(
            (item for item in prepared if item.pilot), start=1
        )
    ]
    fake = _FakeClient(existing)
    monkeypatch.setattr(
        importer, "WanshitongAdminClient", lambda _base_url: fake
    )

    report = importer.run_import(
        _arguments(tmp_path, corpus, manifest, resume=True)
    )

    assert report["skipped_retrievable"] == 4
    assert report["retrievable"] == 4
    assert report["failed"] == 0
    assert fake.upload_count == 0


def test_refresh_templates_only_creates_versions_for_existing_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = _load_importer()
    corpus, manifest = _build_corpus(tmp_path)
    prepared = importer.load_and_validate_manifest(manifest, corpus)
    existing = [
        {
            "document_id": f"doc_{index:032x}",
            "source_relative_path": item.api_path,
            "retrievable": True,
        }
        for index, item in enumerate(prepared, start=1)
    ]
    fake = _FakeClient(existing)
    monkeypatch.setattr(importer, "WanshitongAdminClient", lambda _url: fake)
    arguments = _arguments(tmp_path, corpus, manifest)
    arguments.pilot_only = False
    arguments.refresh_templates = True

    report = importer.run_import(arguments)

    assert report["selected_count"] == 13
    assert report["refreshed_template_catalog"] == 13
    assert report["ingestion_succeeded"] == 13
    assert report["failed"] == 0
    assert fake.template_refresh_count == 13
    assert fake.upload_count == 0


def test_explicit_terminal_recovery_creates_versions_without_deleting_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = _load_importer()
    corpus, manifest = _build_corpus(tmp_path)
    prepared = importer.load_and_validate_manifest(manifest, corpus)
    existing = [
        {
            "document_id": f"doc_{index:032x}",
            "source_relative_path": item.api_path,
            "retrievable": False,
            "latest_job": {
                "job_id": f"job_{index:032x}",
                "state": "failed_terminal",
            },
        }
        for index, item in enumerate(
            (item for item in prepared if item.pilot), start=1
        )
    ]
    fake = _FakeClient(existing)
    monkeypatch.setattr(
        importer, "WanshitongAdminClient", lambda _base_url: fake
    )

    report = importer.run_import(
        _arguments(
            tmp_path,
            corpus,
            manifest,
            resume=True,
            recover_terminal=True,
        )
    )

    assert report["recovered_terminal"] == 4
    assert report["ingestion_succeeded"] == 4
    assert report["retrievable"] == 4
    assert report["failed"] == 0
    assert fake.upload_count == 0
    assert fake.version_count == 4
