"""断网启动前的本地资源完整性检查。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from rag_app.chunking import HuggingFaceTokenCounter
from rag_app.runtime import load_pipeline
from rag_app.settings import RetrievalSettings

__all__ = ["AssetCheckReport", "AssetPaths", "verify_offline_assets"]

_MANIFEST_LINE = re.compile(
    r"^(?P<digest>[0-9a-f]{64})  (?P<path>[^\r\n]+)$"
)
_REMOTE_RESOURCE_MARKERS = ("http://", "https://", "src=\"//", "href=\"//")


@dataclass(frozen=True, slots=True)
class AssetCheckReport:
    """一次无网络资源自检的非敏感结果。"""

    verified_files: int
    retrieval_state: str
    pipeline_fingerprint: str
    tokenizer_probe_tokens: int


@dataclass(frozen=True, slots=True)
class AssetPaths:
    """离线自检使用的全部本地路径。"""

    root: Path
    manifest_path: Path
    pipeline_path: Path
    retrieval_path: Path
    tokenizer_path: Path
    frontend_dir: Path


def verify_offline_assets(paths: AssetPaths) -> AssetCheckReport:
    """校验 SHA、严格配置、tokenizer 与本地前端。

    Args:
        paths: manifest、配置、tokenizer 与前端的本地路径。

    Returns:
        可序列化的资源检查结果。

    Raises:
        ValueError: 路径越界、摘要漂移、重复项或远程资源引用。

    """
    verified = _verify_manifest(
        root=paths.root,
        manifest_path=paths.manifest_path,
    )
    pipeline = load_pipeline(paths.pipeline_path)
    retrieval = RetrievalSettings.load(paths.retrieval_path)
    counter = HuggingFaceTokenCounter(paths.tokenizer_path)
    probe_tokens = counter.count("离线 DOCX RAG 资源自检")
    if probe_tokens <= 0:
        raise ValueError("tokenizer 自检未产生 token。")
    _verify_frontend(paths.frontend_dir)
    return AssetCheckReport(
        verified_files=verified,
        retrieval_state=retrieval.status.value,
        pipeline_fingerprint=pipeline.fingerprint(),
        tokenizer_probe_tokens=probe_tokens,
    )


def _verify_manifest(*, root: Path, manifest_path: Path) -> int:
    """验证资源清单中的路径边界、唯一性与文件摘要。

    Args:
        root: 所有清单条目必须位于其中的资源根目录。
        manifest_path: 使用双空格分隔摘要和相对路径的清单。

    Returns:
        已成功校验的唯一资源文件数量。

    Raises:
        ValueError: 清单为空、格式无效、路径越界或摘要不一致。

    """
    resolved_root = root.resolve(strict=True)
    rows = manifest_path.read_text(encoding="utf-8").splitlines()
    if not rows:
        raise ValueError("资源 SHA256 清单为空。")
    seen: set[str] = set()
    for row in rows:
        match = _MANIFEST_LINE.fullmatch(row)
        if match is None:
            raise ValueError("资源 SHA256 清单格式无效。")
        relative = match.group("path")
        if relative in seen:
            raise ValueError("资源 SHA256 清单含重复路径。")
        seen.add(relative)
        candidate = (resolved_root / relative).resolve(strict=True)
        if (
            not candidate.is_relative_to(resolved_root)
            or not candidate.is_file()
        ):
            raise ValueError("资源路径越出允许根目录或不是文件。")
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != match.group("digest"):
            raise ValueError(f"资源 SHA256 不一致：{relative}")
    return len(seen)


def _verify_frontend(frontend_dir: Path) -> None:
    """确认必需前端文件不存在远程资源引用。

    Args:
        frontend_dir: 包含固定前端文件的本地目录。

    Returns:
        无返回值。

    Raises:
        ValueError: 任一前端文件引用远程资源。

    """
    for name in ("index.html", "styles.css", "app.js"):
        path = frontend_dir / name
        text = path.read_text(encoding="utf-8")
        normalized = text.casefold()
        if any(marker in normalized for marker in _REMOTE_RESOURCE_MARKERS):
            raise ValueError(f"前端资源含远程引用：{name}")
