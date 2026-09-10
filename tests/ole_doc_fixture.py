"""公开合成 OLE Word 容器夹具；不包含真实文档内容。"""

from __future__ import annotations

import struct

_FREE_SECTOR = 0xFFFFFFFF
_END_OF_CHAIN = 0xFFFFFFFE
_FAT_SECTOR = 0xFFFFFFFD
_SECTOR_BYTES = 512


def build_ole_word_container(
    *,
    stream_name: str = "WordDocument",
    marker: int = 0,
) -> bytes:
    """构造仅用于格式门禁和注入式 converter 测试的最小 CFB。

    Returns:
        含指定 directory stream 名称的确定性 OLE 字节。

    """
    header = bytearray(_SECTOR_BYTES)
    header[:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 24, 0x003E)
    struct.pack_into("<H", header, 26, 3)
    struct.pack_into("<H", header, 28, 0xFFFE)
    struct.pack_into("<H", header, 30, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<I", header, 40, 0)
    struct.pack_into("<I", header, 44, 1)
    struct.pack_into("<I", header, 48, 0)
    struct.pack_into("<I", header, 56, 4096)
    struct.pack_into("<I", header, 60, _END_OF_CHAIN)
    struct.pack_into("<I", header, 64, 0)
    struct.pack_into("<I", header, 68, _END_OF_CHAIN)
    struct.pack_into("<I", header, 72, 0)
    for offset in range(76, _SECTOR_BYTES, 4):
        struct.pack_into("<I", header, offset, _FREE_SECTOR)
    struct.pack_into("<I", header, 76, 1)

    directory = bytearray(_SECTOR_BYTES)
    _write_directory_entry(directory, 0, "Root Entry", object_type=5)
    _write_directory_entry(directory, 1, stream_name, object_type=2)
    struct.pack_into("<Q", directory, 100, marker)

    fat = bytearray(b"\xff" * _SECTOR_BYTES)
    struct.pack_into("<I", fat, 0, _END_OF_CHAIN)
    struct.pack_into("<I", fat, 4, _FAT_SECTOR)
    return bytes(header + directory + fat)


def _write_directory_entry(
    directory: bytearray,
    index: int,
    name: str,
    *,
    object_type: int,
) -> None:
    offset = index * 128
    encoded = f"{name}\x00".encode("utf-16le")
    directory[offset : offset + len(encoded)] = encoded
    struct.pack_into("<H", directory, offset + 64, len(encoded))
    directory[offset + 66] = object_type
    directory[offset + 67] = 1
    struct.pack_into("<I", directory, offset + 68, _FREE_SECTOR)
    struct.pack_into("<I", directory, offset + 72, _FREE_SECTOR)
    struct.pack_into("<I", directory, offset + 76, _FREE_SECTOR)
    struct.pack_into("<I", directory, offset + 116, _END_OF_CHAIN)


__all__ = ["build_ole_word_container"]
