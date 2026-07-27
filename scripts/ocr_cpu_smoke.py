"""用本地静态模型对 DOCX 内一张真实位图执行 CPU OCR 冒烟。"""

from __future__ import annotations

import argparse
import json
import os
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from rag_app.ocr.paddle_engine import PaddleOcrEngine

_SUPPORTED_SUFFIXES = frozenset({".jpeg", ".jpg", ".png"})
_MAX_MEDIA_BYTES = 10 * 1024 * 1024


def main(arguments: Sequence[str] | None = None) -> int:
    """运行一次不输出原图和识别文本的 CPU OCR。

    Args:
        arguments: 可选命令行参数；`None` 表示进程参数。

    Returns:
        模型加载和一次真实图片识别成功时返回 0。

    Raises:
        ValueError: DOCX 中没有符合限制的 PNG/JPEG 图片。

    """
    options = _arguments(arguments)
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    docx_path = (
        options.docx
        if options.docx is not None
        else _first_docx(options.docx_root)
    )
    image_bytes, media_type = _first_supported_media(docx_path)
    started = time.perf_counter()
    engine = PaddleOcrEngine.from_model_directories(
        detection_model_dir=options.detection_model_dir,
        recognition_model_dir=options.recognition_model_dir,
        device="cpu",
    )
    loaded_seconds = time.perf_counter() - started
    inference_started = time.perf_counter()
    result = engine.recognize(image_bytes)
    inference_seconds = time.perf_counter() - inference_started
    mean_confidence = (
        sum(line.confidence for line in result.lines) / len(result.lines)
        if result.lines
        else 0.0
    )
    print(
        json.dumps(
            {
                "device": "cpu",
                "inference_seconds": round(inference_seconds, 3),
                "line_count": len(result.lines),
                "mean_confidence": round(mean_confidence, 6),
                "media_bytes": len(image_bytes),
                "media_type": media_type,
                "model_load_seconds": round(loaded_seconds, 3),
                "non_whitespace_characters": sum(
                    len("".join(line.text.split())) for line in result.lines
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _first_supported_media(docx_path: Path) -> tuple[bytes, str]:
    with zipfile.ZipFile(docx_path) as archive:
        members = sorted(
            archive.infolist(),
            key=lambda item: item.filename,
        )
        for member in members:
            pure = PurePosixPath(member.filename)
            suffix = pure.suffix.lower()
            if (
                member.is_dir()
                or pure.parts[:2] != ("word", "media")
                or ".." in pure.parts
                or suffix not in _SUPPORTED_SUFFIXES
                or not 0 < member.file_size <= _MAX_MEDIA_BYTES
            ):
                continue
            media_type = "image/png" if suffix == ".png" else "image/jpeg"
            image_bytes = archive.read(member)
            if len(image_bytes) != member.file_size:
                raise ValueError("DOCX 图片解压长度不一致。")
            return image_bytes, media_type
    raise ValueError("DOCX 中没有符合限制的 PNG/JPEG 图片。")


def _first_docx(root: Path | None) -> Path:
    if root is None:
        raise ValueError("必须提供 DOCX 文件或根目录。")
    candidates = sorted(root.rglob("*.docx"), key=lambda path: path.as_posix())
    if not candidates:
        raise ValueError("根目录中没有 DOCX 文件。")
    return candidates[0]


def _arguments(arguments: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--docx", type=Path)
    source.add_argument("--docx-root", type=Path)
    parser.add_argument(
        "--detection-model-dir",
        type=Path,
        default=Path(
            "deployment/ocr/assets/models/PP-OCRv5_server_det_infer"
        ),
    )
    parser.add_argument(
        "--recognition-model-dir",
        type=Path,
        default=Path(
            "deployment/ocr/assets/models/PP-OCRv5_server_rec_infer"
        ),
    )
    return parser.parse_args(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
