#!/usr/bin/env python3
"""为旧 DOC 模板登记目录提示；绝不读取或上传原件正文。"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
import unicodedata
from pathlib import Path, PurePosixPath

from import_docx import (
    DOCX_MEDIA_TYPE,
    ApiError,
    ImportContractError,
    PreparedDocument,
    WanshitongAdminClient,
    _existing_by_path,
    _literal_path_segments,
    _read_bootstrap_token,
    _validate_docx,
    _wait_for_document,
    _write_report,
)


def catalog_identity(relative_path: str) -> tuple[str, str, dict[str, object]]:
    """从旧 DOC 的路径推导目录项身份，不接触文件内容。"""
    parts = _literal_path_segments(relative_path)
    if not parts[-1].casefold().endswith(".doc"):
        raise ImportContractError("目录项仅适用于旧 DOC 模板。")
    normalized = tuple(unicodedata.normalize("NFKC", part) for part in parts)
    if not any("模板" in part for part in normalized):
        raise ImportContractError("源文件不在模板目录中。")
    if normalized[0] not in {"01 科管", "02 人力", "03 综合", "04 开发中心"}:
        raise ImportContractError("模板不属于固定四空间。")
    alias = "/".join((*normalized[:-1], normalized[-1] + "x"))
    title = html.unescape(PurePosixPath(normalized[-1]).stem) + "（原件 .doc）"
    return alias, title, {
        "source_relative_path": alias,
        "department_name": normalized[0],
        "category_path": list(normalized[1:-1]),
        "document_title": title,
        "topic_keys": [],
    }


def prepare_catalog(
    root: Path, relative_path: str, artifact: Path
) -> dict[str, object]:
    """仅检查原件路径与类型；根据文件名生成全新的 DOCX 目录项。"""
    parts = _literal_path_segments(relative_path)
    alias, title, _ = catalog_identity(relative_path)
    if root.is_symlink() or not root.is_dir():
        raise ImportContractError("语料根必须是普通目录。")
    source = root.resolve(strict=True)
    for part in parts:
        source /= part
        if source.is_symlink():
            raise ImportContractError("源文件路径不能包含 symlink。")
    if not source.is_file():
        raise ImportContractError("旧 DOC 模板不存在。")
    if artifact.exists() or artifact.is_symlink():
        raise ImportContractError("目录项产物已存在，不能覆盖。")
    if artifact.suffix.casefold() != ".docx" or not artifact.parent.is_dir():
        raise ImportContractError("目录项产物必须是新建的 DOCX 文件。")
    # 60 主机只执行 upload，不要求它安装本地 DOCX 生成依赖。
    from rag_app.wanshitong.template_catalog import (  # noqa: PLC0415
        searchable_upload_content,
    )

    content = searchable_upload_content(
        b"", source_relative_path=alias, document_title=title
    )
    artifact.write_bytes(content)
    return {
        "source_relative_path": relative_path,
        "catalog_source_relative_path": alias,
        "artifact_sha256": hashlib.sha256(content).hexdigest(),
        "original_bytes_read": 0,
    }


def upload_catalog(
    *,
    base_url: str,
    token_file: Path,
    artifact: Path,
    relative_path: str,
    report: Path,
) -> dict[str, object]:
    """经固定管理员 API 注册目录项，不绕过生命周期和索引。"""
    alias, _, metadata = catalog_identity(relative_path)
    _validate_docx(artifact, "legacy-template-catalog")
    content = artifact.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    client = WanshitongAdminClient(base_url)
    client.login(_read_bootstrap_token(token_file))
    existing = _existing_by_path(client.list_documents()).get(alias)
    outcome: dict[str, object] = {
        "source_relative_path": relative_path,
        "catalog_source_relative_path": alias,
        "artifact_sha256": digest,
        "original_bytes_read": 0,
        "retrievable": False,
    }
    if existing is not None:
        if existing.get("retrievable") is not True:
            raise ImportContractError("既有目录项不可检索，需先诊断其 Job。")
        outcome.update(action="skipped_retrievable", retrievable=True)
    else:
        identity = hashlib.sha256(
            (alias + "\0" + digest).encode("utf-8")
        ).hexdigest()
        document = PreparedDocument(
            control_id="legacy-template-catalog",
            literal_path=relative_path,
            api_path=alias,
            display_name=PurePosixPath(alias).name,
            media_type=DOCX_MEDIA_TYPE,
            pilot=False,
            file_path=artifact,
            sha256=digest,
            idempotency_key="wb07r-legacy-template-" + identity,
            metadata=metadata,
        )
        receipt = client.upload(document)
        created = receipt.get("document")
        job = receipt.get("job")
        if not isinstance(created, dict) or not isinstance(job, dict):
            raise ImportContractError("目录项上传回执缺少文档或 Job。")
        document_id, job_id = created.get("document_id"), job.get("job_id")
        if not isinstance(document_id, str) or not isinstance(job_id, str):
            raise ImportContractError("目录项上传回执身份无效。")
        final_job, final_document, _ = _wait_for_document(
            client,
            document_id=document_id,
            job_id=job_id,
            timeout_seconds=1800,
            poll_seconds=2.0,
            allow_retry=True,
        )
        outcome.update(
            action="uploaded",
            document_id=document_id,
            job_id=job_id,
            final_job_state=final_job.get("state"),
            retrievable=final_document.get("retrievable") is True,
        )
    _write_report(report, outcome)
    return outcome


def main(argv: list[str] | None = None) -> int:
    """分离本地生成与内网注册，避免服务器依赖外网。"""
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    prepare = subcommands.add_parser("prepare")
    prepare.add_argument("--root", type=Path, required=True)
    prepare.add_argument("--source-relative-path", required=True)
    prepare.add_argument("--artifact", type=Path, required=True)
    upload = subcommands.add_parser("upload")
    upload.add_argument("--base-url", required=True)
    upload.add_argument("--bootstrap-token-file", type=Path, required=True)
    upload.add_argument("--source-relative-path", required=True)
    upload.add_argument("--artifact", type=Path, required=True)
    upload.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_catalog(
                args.root, args.source_relative_path, args.artifact
            )
        else:
            result = upload_catalog(
                base_url=args.base_url,
                token_file=args.bootstrap_token_file,
                artifact=args.artifact,
                relative_path=args.source_relative_path,
                report=args.report,
            )
    except (ImportContractError, ApiError, OSError) as error:
        print(
            f"catalog=failed reason={type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if args.command == "prepare" or result["retrievable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
