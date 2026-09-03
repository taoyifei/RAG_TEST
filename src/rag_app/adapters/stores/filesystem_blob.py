"""受控 data root 内的 content-addressed Filesystem Blob Store。"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import Conflict, ValidationFailed
from rag_app.core.models import BlobPhysicalAudit
from rag_app.core.ports import BlobPutResult, BlobReadResult, BlobWriteRequest

_SHA256_LENGTH = 64
_CAS_PREFIX_LENGTH = 2


class FilesystemBlobStore:
    """用临时文件和原子 hard link 提交的本地 Blob Store。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.BLOB_STORE,
        name="filesystem-blob",
        version="1",
        mode=ProviderMode.LOCAL,
    )

    def __init__(
        self,
        data_root: str | Path,
        *,
        fsync: bool = True,
        media_type_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        """创建受控目录且拒绝 data root symlink。

        Args:
            data_root: P06 专用本地数据根目录。
            fsync: 提交前是否同步文件内容。
            media_type_lookup: 可选 catalog 媒体类型读取器。

        Returns:
            无返回值。

        Raises:
            ValidationFailed: 根目录或子目录是 symlink。

        """
        root = Path(data_root)
        if root.exists() and root.is_symlink():
            raise ValidationFailed(
                "Blob data root 禁止 symlink。", stage="blob.path"
            )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root = root.resolve(strict=True)
        self._blob_root = self._root / "blobs" / "sha256"
        self._temporary_root = self._root / "tmp"
        for directory in (self._blob_root, self._temporary_root):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink():
                raise ValidationFailed(
                    "Blob 受控子目录禁止 symlink。",
                    stage="blob.path",
                )
        self._fsync = fsync
        self._media_type_lookup = media_type_lookup
        self._known_media_types: dict[str, str] = {}
        self._closed = False

    def put_if_absent(self, request: BlobWriteRequest) -> BlobPutResult:
        """验证身份并以原子 hard link 提交 Blob。

        Args:
            request: artifact ID、摘要、媒体类型和字节。

        Returns:
            新建时 CREATED，已存在且一致时 EXISTING。

        Raises:
            ValidationFailed: ID、摘要、路径或现有对象无效。
            Conflict: 同一 Artifact ID 对应不同内容。

        """
        self._ensure_open()
        digest = hashlib.sha256(request.content).hexdigest()
        expected_id = f"sha256:{request.content_sha256}"
        if request.blob_id != expected_id or digest != request.content_sha256:
            raise ValidationFailed(
                "Blob ID、摘要与内容不一致。", stage="blob.write"
            )
        target = self._path(request.blob_id)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._ensure_safe_directory(target.parent)
        temporary = self._temporary_root / uuid.uuid4().hex
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(request.content)
                stream.flush()
                if self._fsync:
                    os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
                outcome = BlobPutResult.CREATED
            except FileExistsError:
                self._verify_existing(
                    target, request.content_sha256, len(request.content)
                )
                outcome = BlobPutResult.EXISTING
            if self._fsync:
                self._sync_directory(target.parent)
            self._known_media_types[request.blob_id] = request.media_type
            return outcome
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, blob_id: str) -> BlobReadResult | None:
        """回读并重新校验受控 locator 与摘要。

        Args:
            blob_id: content-addressed Artifact 对象 ID。

        Returns:
            找到时返回完整 Blob，否则为 None。

        """
        self._ensure_open()
        target = self._path(blob_id)
        if not target.exists():
            return None
        if target.is_symlink() or not target.is_file():
            raise ValidationFailed(
                "Blob locator 不是受控普通文件。", stage="blob.read"
            )
        content = target.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if blob_id != f"sha256:{digest}":
            raise ValidationFailed("Blob 读取摘要不匹配。", stage="blob.read")
        media_type = self._known_media_types.get(blob_id)
        if media_type is None and self._media_type_lookup is not None:
            media_type = self._media_type_lookup(blob_id)
        return BlobReadResult(
            blob_id=blob_id,
            content_sha256=digest,
            media_type=media_type or "application/octet-stream",
            content=content,
        )

    def exists(self, blob_id: str) -> bool:
        """判断受控 Blob 是否存在。

        Args:
            blob_id: content-addressed Artifact 对象 ID。

        Returns:
            存在普通文件时为 True。

        """
        self._ensure_open()
        target = self._path(blob_id)
        return target.exists() and target.is_file() and not target.is_symlink()

    def delete(self, blob_id: str) -> None:
        """幂等删除由上层已确认无引用的 Blob。

        Args:
            blob_id: content-addressed Artifact 对象 ID。

        Returns:
            无返回值。

        """
        self._ensure_open()
        target = self._path(blob_id)
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise ValidationFailed(
                "Blob 删除目标不是普通文件。", stage="blob.delete"
            )
        target.unlink(missing_ok=True)
        self._known_media_types.pop(blob_id, None)

    def audit_inventory(self) -> tuple[BlobPhysicalAudit, ...]:
        """失败关闭地扫描受控 `blobs/sha256` 两级布局。

        Args:
            无参数；扫描当前 Store 根。

        Returns:
            已验证摘要且不含路径的稳定物理清单。

        Raises:
            ValidationFailed: 发现 symlink、非普通文件或异常布局。

        """
        self._ensure_open()
        audited: list[BlobPhysicalAudit] = []
        for prefix in sorted(self._blob_root.iterdir()):
            if (
                prefix.is_symlink()
                or not prefix.is_dir()
                or len(prefix.name) != _CAS_PREFIX_LENGTH
                or any(
                    character not in "0123456789abcdef"
                    for character in prefix.name
                )
            ):
                raise ValidationFailed(
                    "Blob inventory 发现异常 prefix。", stage="blob.audit"
                )
            for target in sorted(prefix.iterdir()):
                digest = target.name
                if (
                    target.is_symlink()
                    or not target.is_file()
                    or len(digest) != _SHA256_LENGTH
                    or not digest.startswith(prefix.name)
                    or any(
                        character not in "0123456789abcdef"
                        for character in digest
                    )
                ):
                    raise ValidationFailed(
                        "Blob inventory 发现异常对象。", stage="blob.audit"
                    )
                observed = hashlib.sha256(target.read_bytes()).hexdigest()
                if observed != digest:
                    raise ValidationFailed(
                        "Blob inventory 摘要不匹配。", stage="blob.audit"
                    )
                audited.append(
                    BlobPhysicalAudit(
                        blob_id=f"sha256:{digest}",
                        content_sha256=digest,
                        size_bytes=target.stat().st_size,
                        reason_code="OK",
                    )
                )
        return tuple(audited)

    def close(self) -> None:
        """幂等关闭 Store。

        Args:
            无参数；清理进程内媒体类型缓存。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self._known_media_types.clear()

    def locator(self, blob_id: str) -> str:
        """返回可写入 catalog 的受控相对 locator。

        Args:
            blob_id: content-addressed Artifact 对象 ID。

        Returns:
            POSIX 形式相对 locator。

        """
        return self._path(blob_id).relative_to(self._root).as_posix()

    def _path(self, blob_id: str) -> Path:
        if not blob_id.startswith("sha256:"):
            raise ValidationFailed(
                "Blob ID 必须使用 sha256 scheme。", stage="blob.path"
            )
        digest = blob_id.removeprefix("sha256:")
        if len(digest) != _SHA256_LENGTH or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValidationFailed("Blob SHA-256 格式无效。", stage="blob.path")
        target = (self._blob_root / digest[:2] / digest).resolve(strict=False)
        if not target.is_relative_to(self._root):
            raise ValidationFailed(
                "Blob locator 逃逸 data root。", stage="blob.path"
            )
        return target

    def _verify_existing(self, path: Path, digest: str, size: int) -> None:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != size
        ):
            raise Conflict("现有 Artifact 与请求不一致。", stage="blob.write")
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed != digest:
            raise Conflict("现有 Artifact 与请求不一致。", stage="blob.write")

    def _ensure_safe_directory(self, path: Path) -> None:
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(
            self._root
        ):
            raise ValidationFailed(
                "Blob 目录逃逸 data root。", stage="blob.path"
            )

    def _sync_directory(self, directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("FilesystemBlobStore 已关闭。")


__all__ = ["FilesystemBlobStore"]
