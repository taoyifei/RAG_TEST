"""只读冻结 WB08R-03 V14 的 8289 候选与评测身份。"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

_CONTAINER = "wanshitong-wb08r01-app"
_RUNNER_RELATIVE = Path("evaluation/wanshitong/v2/run_wb08r03g_v8.py")
_RUNNER_SHA256 = (
    "539cce86acfcbee57f40c11307448a07483758ed1c0d715b4766e6a2463c1115"
)
_BASE_IMAGE_ID = (
    "sha256:621acc792c12169d347fde03a8cfb06dff4ca033bf4867171d35bc46f284b75c"
)
_BASE_TAG = "wb08r03f-bd00c56"
_PROBE = r"""
import hashlib
import json
import sqlite3
from pathlib import Path

import rag_app
from rag_app._build_revision import SOURCE_REVISION
from rag_app.core.models.generation_packet import (
    EVIDENCE_IDENTITY_REVISION,
    PREPARED_PACKET_REVISION,
)
from rag_app.core.models.query_plan import (
    GROUNDED_CLAIM_SCHEMA_REVISION,
    QUERY_PLAN_SCHEMA_REVISION,
)
from rag_app.product.asset_manifest import verify_product_asset_manifest


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode()


expected_code, knowledge_base_id = __import__("sys").argv[1:]
if SOURCE_REVISION != expected_code:
    raise RuntimeError("RUNTIME_CODE_MISMATCH")
root = Path(rag_app.__file__).parent
files = [
    {
        "path": str(path.relative_to(root)),
        "sha256": digest(path.read_bytes()),
    }
    for path in sorted(root.rglob("*"))
    if path.is_file()
    and "__pycache__" not in path.parts
    and path.suffix not in {".pyc", ".pyo"}
]
assets = verify_product_asset_manifest(
    root=Path("/app"),
    manifest_path=Path("/app/product-assets.json"),
    expected_source_revision=expected_code,
)
dependency_lock = Path("/app/requirements.runtime.lock")
if not dependency_lock.is_file():
    raise RuntimeError("RUNTIME_DEPENDENCY_LOCK_MISSING")
database = sqlite3.connect(
    "file:/data/universal-rag.sqlite3?mode=ro", uri=True
)
database.row_factory = sqlite3.Row
database.execute("PRAGMA query_only=ON")
row = database.execute(
    "SELECT k.active_revision_id, r.index_fingerprint, "
    "r.expected_document_count, r.expected_chunk_count, "
    "p.profile_revision_id, p.index_semantic_fingerprint, "
    "p.serving_fingerprint "
    "FROM knowledge_bases k "
    "JOIN index_revisions r "
    "ON r.index_revision_id=k.active_revision_id "
    "JOIN retrieval_profile_revisions p "
    "ON p.knowledge_base_id=k.knowledge_base_id AND p.status='active' "
    "WHERE k.knowledge_base_id=? AND k.deleted_at IS NULL",
    (knowledge_base_id,),
).fetchone()
if row is None:
    raise RuntimeError("ACTIVE_RUNTIME_IDENTITY_MISSING")
documents = database.execute(
    "SELECT count(*) FROM revision_documents WHERE revision_id=?",
    (row["active_revision_id"],),
).fetchone()[0]
chunks = database.execute(
    "SELECT count(*) FROM chunks WHERE revision_id=?",
    (row["active_revision_id"],),
).fetchone()[0]
if (documents, chunks) != (
    row["expected_document_count"],
    row["expected_chunk_count"],
):
    raise RuntimeError("ACTIVE_RUNTIME_COUNT_MISMATCH")
print(
    json.dumps(
        {
            "code_sha": SOURCE_REVISION,
            "runtime_tree_sha256": digest(canonical(files)),
            "runtime_file_count": len(files),
            "asset_manifest_sha256": assets.manifest_sha256,
            "asset_file_count": assets.verified_files,
            "dependency_lock_sha256": digest(dependency_lock.read_bytes()),
            "active_index_revision_id": row["active_revision_id"],
            "index_fingerprint": row["index_fingerprint"],
            "profile_revision_id": row["profile_revision_id"],
            "index_semantic_fingerprint": row[
                "index_semantic_fingerprint"
            ],
            "serving_fingerprint": row["serving_fingerprint"],
            "document_count": documents,
            "chunk_count": chunks,
            "schema_revision": GROUNDED_CLAIM_SCHEMA_REVISION,
            "query_plan_revision": QUERY_PLAN_SCHEMA_REVISION,
            "evidence_identity_revision": EVIDENCE_IDENTITY_REVISION,
            "packet_revision": PREPARED_PACKET_REVISION,
        },
        sort_keys=True,
    )
)
"""


def _sha256(path: Path) -> str:
    """计算文件 SHA-256。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: object) -> str:
    """计算规范 JSON 摘要。"""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _docker_json(arguments: list[str]) -> Any:  # noqa: ANN401
    """执行固定只读 Docker 子命令并解析 JSON。"""
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/docker", *arguments],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return json.loads(result.stdout)


def _write_private_json(path: Path, value: object) -> None:
    """独占写入所有者可读的 JSON。"""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def freeze(  # noqa: PLR0912
    runner_root: Path,
    output: Path,
    *,
    expected_code: str,
    expected_image_id: str,
) -> dict[str, Any]:
    """冻结候选、索引、配置和数据集的同一版本身份。"""
    if re.fullmatch(r"[0-9a-f]{40}", expected_code) is None:
        raise ValueError("EXPECTED_CODE_INVALID")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", expected_image_id) is None:
        raise ValueError("EXPECTED_IMAGE_ID_INVALID")
    root = runner_root.resolve(strict=True)
    target = output.resolve()
    if target.exists() or root == target or root in target.parents:
        raise ValueError("FRESH_PRIVATE_OUTPUT_REQUIRED")
    runner_path = root / _RUNNER_RELATIVE
    if _sha256(runner_path) != _RUNNER_SHA256:
        raise ValueError("RUNNER_DIGEST_CHANGED")
    sys.path.insert(0, str(root))
    runner = importlib.import_module("evaluation.wanshitong.v2.run_wb08r03g_v8")
    module_file = runner.__file__
    if (
        not isinstance(module_file, str)
        or Path(module_file).resolve() != runner_path
    ):
        raise ValueError("RUNNER_IMPORT_IDENTITY_CHANGED")
    truth, cases, evidence = runner.legacy.load_truth()
    before = runner.observe_runtime_guard()
    inspected = _docker_json(["inspect", _CONTAINER])[0]
    if (
        before["code_sha"] != expected_code
        or before["image_id"] != expected_image_id
        or inspected["Id"] != before["container_id"]
    ):
        raise ValueError("CANDIDATE_IDENTITY_MISMATCH")
    if inspected["NetworkSettings"]["Ports"].get("8088/tcp") != [
        {"HostIp": "127.0.0.1", "HostPort": "8289"}
    ]:
        raise ValueError("CANDIDATE_PORT_IDENTITY_CHANGED")
    labels = inspected["Config"]["Labels"]
    if (
        labels.get("org.opencontainers.image.revision") != expected_code
        or labels.get("org.opencontainers.image.wb08r.base") != _BASE_TAG
    ):
        raise ValueError("CANDIDATE_LABEL_MISMATCH")
    base = _docker_json(
        ["image", "inspect", "rag-test-wanshitong:" + _BASE_TAG]
    )[0]
    if base["Id"] != _BASE_IMAGE_ID:
        raise ValueError("BASE_IMAGE_ID_CHANGED")
    command = [
        "exec",
        _CONTAINER,
        "python",
        "-B",
        "-c",
        _PROBE,
        expected_code,
        truth["knowledge_base_id"],
    ]
    observed = _docker_json(command)
    repeated = _docker_json(command)
    after = runner.observe_runtime_guard()
    if observed != repeated or before != after:
        raise ValueError("RUNTIME_CHANGED_DURING_FREEZE")
    if (
        observed["active_index_revision_id"]
        != truth["active_index_revision_id"]
    ):
        raise ValueError("GOLD_ACTIVE_INDEX_MISMATCH")
    dataset_files = {
        str(path.relative_to(root)): _sha256(path)
        for path in sorted((root / "evaluation/wanshitong/v2").glob("*.ndjson"))
    }
    dataset_manifest = root / "evaluation/wanshitong/v2/dataset-manifest.json"
    dataset_files[str(dataset_manifest.relative_to(root))] = _sha256(
        dataset_manifest
    )
    truth_root = root / "evaluation/wanshitong/wb08r03f-truth-v1"
    for name in ("manifest.json", "cases.ndjson", "evidence_truth.ndjson"):
        path = truth_root / name
        dataset_files[str(path.relative_to(root))] = _sha256(path)
    configuration_sha256 = _canonical_sha256(
        {
            "database_behavior_sha256": before["database_behavior_sha256"],
            "environment_sha256": before["environment_sha256"],
            "active_index_revision_id": observed["active_index_revision_id"],
            "serving_fingerprint": observed["serving_fingerprint"],
        }
    )
    bundle = cast(
        dict[str, Any],
        runner.bind_version(
            {
                **observed,
                **before,
                "container": _CONTAINER,
                "base_digest": _BASE_IMAGE_ID,
                "configuration_sha256": configuration_sha256,
                "dataset_revision": truth["source_dataset_revision"]
                + ":"
                + truth["truth_revision"],
                "dataset_sha256": _canonical_sha256(dataset_files),
                "dataset_files_sha256": dataset_files,
                "gold_case_count": len(cases),
                "gold_evidence_count": len(evidence),
                "runner_sha256": _sha256(runner_path),
                "provider_calls": 0,
                "business_requests": 0,
            }
        ),
    )
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.parent.stat().st_mode & 0o077:
        raise ValueError("PRIVATE_OUTPUT_DIRECTORY_PERMISSIONS")
    _write_private_json(target, bundle)
    return bundle


def main() -> None:
    """解析评测根目录、候选身份和私有输出路径。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-code", required=True)
    parser.add_argument("--expected-image-id", required=True)
    args = parser.parse_args()
    bundle = freeze(
        args.runner_root,
        args.output,
        expected_code=args.expected_code,
        expected_image_id=args.expected_image_id,
    )
    print(
        json.dumps(
            {
                "status": "FROZEN",
                "version_bundle_sha256": bundle["version_bundle_sha256"],
                "provider_calls": bundle["provider_calls"],
                "business_requests": bundle["business_requests"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
