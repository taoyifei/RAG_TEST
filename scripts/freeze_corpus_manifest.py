"""冻结、验证并按 manifest 复制 DOCX corpus。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

_CORPUS_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_VERSION = "1"
_HASH_BLOCK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    """单个冻结 DOCX 的稳定身份。"""

    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class CorpusManifest:
    """完整冻结语料的可移植清单。"""

    corpus_digest: str
    corpus_id: str
    document_count: int
    documents: tuple[CorpusDocument, ...]
    schema_version: str
    total_bytes: int


def freeze_corpus_manifest(
    *,
    docs_root: Path,
    corpus_id: str,
    output_path: Path,
) -> CorpusManifest:
    """扫描 DOCX 并原子写出 canonical manifest。

    Args:
        docs_root: 只含待冻结语料的目录。
        corpus_id: 操作员指定的稳定语料标识。
        output_path: 操作员明确指定的输出路径。

    Returns:
        已写入的冻结清单。

    Raises:
        ValueError: 标识、路径或语料集合不安全。

    """
    _validate_corpus_id(corpus_id)
    documents = _discover_documents(docs_root)
    manifest = CorpusManifest(
        corpus_digest=_documents_digest(documents),
        corpus_id=corpus_id,
        document_count=len(documents),
        documents=documents,
        schema_version=_SCHEMA_VERSION,
        total_bytes=sum(document.size for document in documents),
    )
    if output_path.is_symlink():
        raise ValueError("corpus manifest 输出不能是符号链接。")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(_canonical_bytes(manifest))
        temporary.flush()
        os.fsync(temporary.fileno())
    try:
        temporary_path.replace(output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return manifest


def load_corpus_manifest(path: Path) -> CorpusManifest:
    """读取并严格校验 canonical corpus manifest。

    Args:
        path: 操作员提供的 manifest 路径。

    Returns:
        已完成 schema、字段和整体摘要校验的清单。

    Raises:
        ValueError: 文件不是 canonical manifest 或字段无效。

    """
    if not path.is_file() or path.is_symlink():
        raise ValueError("corpus manifest 必须是普通文件。")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("corpus manifest 不是有效 UTF-8 JSON。") from error
    expected_fields = {
        "corpus_digest",
        "corpus_id",
        "document_count",
        "documents",
        "schema_version",
        "total_bytes",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValueError("corpus manifest schema 字段无效。")
    corpus_id = value["corpus_id"]
    if (
        value["schema_version"] != _SCHEMA_VERSION
        or not isinstance(corpus_id, str)
    ):
        raise ValueError("corpus manifest schema version 或 ID 无效。")
    _validate_corpus_id(corpus_id)
    items = value["documents"]
    if not isinstance(items, list):
        raise ValueError("corpus documents 必须是数组。")
    documents: list[CorpusDocument] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "sha256",
            "size",
        }:
            raise ValueError("corpus document schema 无效。")
        relative = item["path"]
        digest = item["sha256"]
        size = item["size"]
        if not isinstance(relative, str):
            raise ValueError("corpus document path 类型无效。")
        _validate_document_path(relative)
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ValueError("corpus document SHA256 无效。")
        if type(size) is not int or size < 0:
            raise ValueError("corpus document size 必须是非负整数。")
        documents.append(
            CorpusDocument(path=relative, sha256=digest, size=size)
        )
    document_tuple = tuple(documents)
    _validate_document_order(document_tuple)
    count = value["document_count"]
    total_bytes = value["total_bytes"]
    corpus_digest = value["corpus_digest"]
    if (
        type(count) is not int
        or type(total_bytes) is not int
        or count != len(document_tuple)
        or total_bytes != sum(item.size for item in document_tuple)
        or not isinstance(corpus_digest, str)
        or corpus_digest != _documents_digest(document_tuple)
    ):
        raise ValueError("corpus count、total_bytes 或 digest 不一致。")
    manifest = CorpusManifest(
        corpus_digest=corpus_digest,
        corpus_id=corpus_id,
        document_count=count,
        documents=document_tuple,
        schema_version=_SCHEMA_VERSION,
        total_bytes=total_bytes,
    )
    if not document_tuple or raw != _canonical_bytes(manifest):
        raise ValueError("corpus manifest 为空或不是 canonical JSON。")
    return manifest


def verify_corpus(
    *,
    docs_root: Path,
    manifest_path: Path,
) -> CorpusManifest:
    """验证目录 DOCX exact set 与冻结清单一致。

    Args:
        docs_root: 待核验的 DOCX 根目录。
        manifest_path: canonical corpus manifest。

    Returns:
        已核验的清单。

    Raises:
        ValueError: 集合、大小或任一摘要不一致。

    """
    manifest = load_corpus_manifest(manifest_path)
    if _discover_documents(docs_root) != manifest.documents:
        raise ValueError("docs exact set、size 或 SHA256 与 manifest 不一致。")
    return manifest


def stage_verified_corpus(
    *,
    docs_root: Path,
    manifest_path: Path,
    destination: Path,
) -> CorpusManifest:
    """按 manifest 顺序复制经过验证的 DOCX。

    Args:
        docs_root: 已冻结的源 DOCX 根目录。
        manifest_path: canonical corpus manifest。
        destination: 必须尚不存在的目标 docs 目录。

    Returns:
        已核验并复制的清单。

    Raises:
        FileExistsError: 目标已存在。
        ValueError: 源或复制结果与 manifest 不一致。

    """
    manifest = verify_corpus(
        docs_root=docs_root,
        manifest_path=manifest_path,
    )
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("corpus staging 目标已存在。")
    destination.mkdir(parents=True)
    source_root = docs_root.resolve(strict=True)
    for document in manifest.documents:
        relative = PurePosixPath(document.path)
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root.joinpath(*relative.parts), target)
    if _discover_documents(destination) != manifest.documents:
        raise ValueError("复制后的 corpus 与 manifest 不一致。")
    return manifest


def _discover_documents(root: Path) -> tuple[CorpusDocument, ...]:
    """递归发现安全普通 DOCX。

    Args:
        root: 待扫描目录。

    Returns:
        按相对 POSIX 路径排序的 DOCX 身份。

    Raises:
        ValueError: 目录含 symlink、Zone.Identifier 或路径冲突。

    """
    if not root.is_dir() or root.is_symlink():
        raise ValueError("docs root 必须是真实目录。")
    resolved_root = root.resolve(strict=True)
    discovered: list[CorpusDocument] = []
    for current, directories, files in os.walk(
        resolved_root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        for name in (*directories, *files):
            if "zone.identifier" in name.casefold():
                raise ValueError("docs 含 Zone.Identifier。")
            if (current_path / name).is_symlink():
                raise ValueError("docs 不能含符号链接。")
        for name in files:
            candidate = current_path / name
            if candidate.suffix.casefold() != ".docx":
                continue
            if not candidate.is_file():
                raise ValueError("DOCX 必须是普通文件。")
            relative = candidate.relative_to(resolved_root).as_posix()
            _validate_document_path(relative)
            discovered.append(
                CorpusDocument(
                    path=relative,
                    sha256=_sha256_file(candidate),
                    size=candidate.stat().st_size,
                )
            )
    documents = tuple(sorted(discovered, key=lambda item: item.path))
    if not documents:
        raise ValueError("docs root 没有 DOCX。")
    _validate_document_order(documents)
    return documents


def _validate_document_order(
    documents: tuple[CorpusDocument, ...],
) -> None:
    """验证路径顺序和大小写折叠唯一性。

    Args:
        documents: 待验证文档身份。

    Returns:
        无。

    Raises:
        ValueError: 路径无序、重复或大小写冲突。

    """
    paths = tuple(document.path for document in documents)
    folded = tuple(path.casefold() for path in paths)
    if (
        paths != tuple(sorted(paths))
        or len(paths) != len(set(paths))
        or len(folded) != len(set(folded))
    ):
        raise ValueError("corpus path 无序、重复或存在 case-fold 冲突。")


def _validate_document_path(value: str) -> None:
    """验证安全 canonical DOCX 相对路径。

    Args:
        value: POSIX 相对路径。

    Returns:
        无。

    Raises:
        ValueError: 路径越界、不规范或扩展名错误。

    """
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or "." in path.parts
        or ".." in path.parts
        or "\\" in value
        or "\x00" in value
        or path.suffix.casefold() != ".docx"
        or any("zone.identifier" in part.casefold() for part in path.parts)
    ):
        raise ValueError("corpus document path 越界或不规范。")


def _validate_corpus_id(value: str) -> None:
    """验证稳定 corpus ID。

    Args:
        value: 操作员指定标识。

    Returns:
        无。

    Raises:
        ValueError: 标识格式无效。

    """
    if _CORPUS_ID.fullmatch(value) is None:
        raise ValueError("corpus ID 必须是 1-64 位安全标识。")


def _documents_digest(documents: tuple[CorpusDocument, ...]) -> str:
    """计算有序文档身份的整体摘要。

    Args:
        documents: 已排序文档身份。

    Returns:
        canonical documents JSON 的 SHA256。

    """
    payload = json.dumps(
        [asdict(document) for document in documents],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_bytes(manifest: CorpusManifest) -> bytes:
    """序列化 canonical manifest。

    Args:
        manifest: 待序列化清单。

    Returns:
        以换行结束的 UTF-8 canonical JSON。

    """
    return (
        json.dumps(
            asdict(manifest),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    """流式计算文件 SHA256。

    Args:
        path: 待读取文件。

    Returns:
        小写十六进制 SHA256。

    """
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _arguments() -> argparse.Namespace:
    """解析 corpus manifest 命令参数。

    Args:
        无参数。

    Returns:
        已解析参数。

    """
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--docs", type=Path, required=True)
    freeze.add_argument("--corpus-id", required=True)
    freeze.add_argument("--output", type=Path, required=True)
    for command in ("verify", "stage"):
        action = commands.add_parser(command)
        action.add_argument("--docs", type=Path, required=True)
        action.add_argument("--manifest", type=Path, required=True)
        if command == "stage":
            action.add_argument("--destination", type=Path, required=True)
    identity = commands.add_parser("id")
    identity.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    """执行冻结、核验、复制或只读 ID 查询。

    Args:
        无参数。

    Returns:
        成功时返回 0；异常导致非零退出。

    """
    arguments = _arguments()
    if arguments.command == "freeze":
        manifest = freeze_corpus_manifest(
            docs_root=arguments.docs,
            corpus_id=arguments.corpus_id,
            output_path=arguments.output,
        )
    elif arguments.command == "verify":
        manifest = verify_corpus(
            docs_root=arguments.docs,
            manifest_path=arguments.manifest,
        )
    elif arguments.command == "stage":
        manifest = stage_verified_corpus(
            docs_root=arguments.docs,
            manifest_path=arguments.manifest,
            destination=arguments.destination,
        )
    else:
        print(load_corpus_manifest(arguments.manifest).corpus_id)
        return 0
    print(
        json.dumps(
            {
                "corpus_digest": manifest.corpus_digest,
                "corpus_id": manifest.corpus_id,
                "document_count": manifest.document_count,
                "total_bytes": manifest.total_bytes,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
