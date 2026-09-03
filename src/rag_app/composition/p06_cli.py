"""P06 持久化索引的安全本地 CLI。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rag_app.application.revision_builder import IngestionDocument
from rag_app.composition.p06_runtime import P06Runtime, build_p06_runtime
from rag_app.composition.profiles import load_profile
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import DocumentRef

_DEFAULT_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
P06_COMMANDS = frozenset(
    {
        "init-data",
        "ingest",
        "job",
        "index-info",
        "index-validate",
        "index-backfill",
        "index-gc",
    }
)


def p06_command(arguments: Sequence[str]) -> int:  # noqa: PLR0911
    """解析并执行一个 P06 管理命令。

    Args:
        arguments: 不含程序名的命令行参数。

    Returns:
        命令成功时返回零。

    """
    parsed = _parser().parse_args(arguments)
    profile = load_profile(parsed.profile)
    with build_p06_runtime(profile, data_dir=parsed.data_dir) as runtime:
        if parsed.command == "init-data":
            project_id = deterministic_id(
                "prj", profile.profile_id, parsed.project_name
            )
            knowledge_base_id = deterministic_id(
                "kb", project_id, parsed.knowledge_base_name
            )
            runtime.control.put_project(project_id, parsed.project_name)
            runtime.control.put_knowledge_base(
                knowledge_base_id,
                project_id,
                parsed.knowledge_base_name,
                profile_id=profile.profile_id,
            )
            _print(
                {
                    "knowledge_base_id": knowledge_base_id,
                    "profile_id": profile.profile_id,
                    "project_id": project_id,
                }
            )
            return 0
        if parsed.command == "ingest":
            return _ingest(runtime, parsed)
        if parsed.command == "job":
            _print(runtime.control.job_summary(parsed.identifier))
            return 0
        if parsed.command == "index-info":
            _print(runtime.control.knowledge_base_summary(parsed.identifier))
            return 0
        if parsed.command == "index-backfill":
            count = runtime.recovery.backfill(
                parsed.identifier, slot_id=parsed.slot
            )
            _print(
                {
                    "backfilled_point_count": count,
                    "revision_id": parsed.identifier,
                    "slot_id": parsed.slot,
                }
            )
            return 0
        if parsed.command == "index-validate":
            runtime.recovery.backfill(parsed.identifier)
            spec = runtime.control.revision_vector_spec(parsed.identifier)
            evidence = runtime.validator.validate(
                spec,
                current_index_fingerprint=runtime.components.index_fingerprint,
            )
            _print(evidence.model_dump(mode="json"))
            return 0
        return _garbage_collect(runtime, parsed)


def _ingest(runtime: P06Runtime, parsed: argparse.Namespace) -> int:
    path = parsed.document_path
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("ingest 输入必须是现有非 symlink DOCX。")
    project_id, profile_id = runtime.control.knowledge_base_scope(
        parsed.knowledge_base_id
    )
    if profile_id != runtime.components.profile.profile_id:
        raise ValueError("知识库 Profile 与当前 CLI Profile 不一致。")
    content = path.read_bytes()
    document_id = parsed.document_id
    if not document_id.startswith("doc_"):
        document_id = deterministic_id(
            "doc", project_id, parsed.knowledge_base_id, document_id
        )
    document_ref = DocumentRef(
        project_id=project_id,
        knowledge_base_id=parsed.knowledge_base_id,
        document_id=document_id,
        display_name=path.name,
    )
    runtime.control.upsert_document(document_ref)
    documents: list[IngestionDocument] = []
    for existing, artifact_id, media_type in runtime.control.active_documents(
        parsed.knowledge_base_id
    ):
        if existing.document_id == document_id:
            continue
        blob = runtime.components.blob_store.read(artifact_id)
        if blob is None:
            raise FileNotFoundError(
                "active DocumentVersion 的 source Artifact 不存在。"
            )
        documents.append(
            IngestionDocument(
                document=existing,
                content=blob.content,
                media_type=media_type,
                extension=".docx",
            )
        )
    documents.append(
        IngestionDocument(
            document=document_ref,
            content=content,
            media_type=_DOCX_MEDIA_TYPE,
            extension=path.suffix.casefold(),
        )
    )
    idempotency_key = (
        parsed.idempotency_key
        or hashlib.sha256(
            "|".join(
                sorted(
                    f"{item.document.document_id}:"
                    f"{hashlib.sha256(item.content).hexdigest()}"
                    for item in documents
                )
            ).encode()
        ).hexdigest()
    )
    result = runtime.builder.build_and_activate(
        project_id=project_id,
        knowledge_base_id=parsed.knowledge_base_id,
        documents=tuple(
            sorted(documents, key=lambda item: item.document.document_id)
        ),
        idempotency_key=idempotency_key,
        budgets=runtime.default_budgets(),
    )
    _print(
        {
            "chunk_count": result.chunk_count,
            "document_count": result.document_count,
            "document_id": document_id,
            "job_id": result.job_id,
            "revision_id": result.revision_id,
            "state": "active",
        }
    )
    return 0


def _garbage_collect(runtime: P06Runtime, parsed: argparse.Namespace) -> int:
    if parsed.plan:
        plan = runtime.garbage_collector.plan(
            protected_retired_count=parsed.protected_retired_count,
            grace_before=(datetime.now(UTC) - timedelta(hours=24)).isoformat(),
        )
        _print(
            {
                "mode": "dry-run",
                "plan_hash": plan.plan_hash,
                "plan_id": plan.plan_id,
                "snapshot": dict(plan.snapshot),
            }
        )
        return 0
    runtime.garbage_collector.apply(parsed.apply)
    _print({"mode": "apply", "plan_id": parsed.apply, "status": "applied"})
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python scripts/dev.py")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", type=Path, default=_DEFAULT_PROFILE)
    common.add_argument("--data-dir", type=Path, default=Path(".data"))
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init-data", parents=[common])
    initialize.add_argument("--project-name", default="Local Project")
    initialize.add_argument("--knowledge-base-name", default="Local KB")
    ingest = commands.add_parser("ingest", parents=[common])
    ingest.add_argument("knowledge_base_id")
    ingest.add_argument("document_path", type=Path)
    ingest.add_argument("--document-id", required=True)
    ingest.add_argument("--idempotency-key")
    for name in ("job", "index-info", "index-validate"):
        command = commands.add_parser(name, parents=[common])
        command.add_argument("identifier")
    backfill = commands.add_parser("index-backfill", parents=[common])
    backfill.add_argument("identifier")
    backfill.add_argument("--slot", required=True)
    gc = commands.add_parser("index-gc", parents=[common])
    mode = gc.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply")
    gc.add_argument("--protected-retired-count", type=int, default=1)
    return parser


def _print(payload: object) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


__all__ = ["P06_COMMANDS", "p06_command"]
