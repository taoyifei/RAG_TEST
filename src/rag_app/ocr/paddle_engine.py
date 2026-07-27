"""使用本地 PP-OCRv5 server 静态模型的 PaddleOCR 引擎。"""

from __future__ import annotations

import importlib
import io
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

from PIL import Image

from rag_app.ocr.models import OcrEngineResult, OcrLine

__all__ = ["PaddleOcrEngine"]

_BBOX_VALUE_COUNT = 4


class _PaddlePipeline(Protocol):
    """PaddleOCR pipeline 的运行时窄接口。"""

    def predict(self, input_data: object) -> Iterable[object]:
        """对一张内存图片执行预测。"""


class PaddleOcrEngine:
    """固定使用 paddle_static 与 PP-OCRv5 server det/rec。"""

    def __init__(self, pipeline: _PaddlePipeline) -> None:
        """保存已加载且禁止运行时换模的 pipeline。

        Args:
            pipeline: 使用本地模型目录初始化的 PaddleOCR pipeline。

        """
        self._pipeline = pipeline

    @classmethod
    def from_model_directories(
        cls,
        *,
        detection_model_dir: Path,
        recognition_model_dir: Path,
        device: str,
    ) -> PaddleOcrEngine:
        """从两个本地静态模型目录创建引擎。

        Args:
            detection_model_dir: PP-OCRv5_server_det_infer 解包目录。
            recognition_model_dir: PP-OCRv5_server_rec_infer 解包目录。
            device: `cpu` 或单个 `gpu:N` 设备。

        Returns:
            已加载两个固定模型的 PaddleOCR 引擎。

        Raises:
            ValueError: 模型目录、设备或 PaddleOCR 模块不符合契约。

        """
        detection = _model_directory(detection_model_dir)
        recognition = _model_directory(recognition_model_dir)
        if device != "cpu" and not (
            device.startswith("gpu:") and device[4:].isdigit()
        ):
            raise ValueError("OCR device 必须是 cpu 或单个 gpu:N。")
        paddleocr_module = importlib.import_module("paddleocr")
        paddleocr_factory = getattr(
            paddleocr_module,
            "PaddleOCR",
            None,
        )
        if not callable(paddleocr_factory):
            raise ValueError("paddleocr 模块缺少 PaddleOCR。")
        pipeline = paddleocr_factory(
            text_detection_model_name="PP-OCRv5_server_det",
            text_detection_model_dir=str(detection),
            text_recognition_model_name="PP-OCRv5_server_rec",
            text_recognition_model_dir=str(recognition),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device=device,
            enable_mkldnn=False,
            enable_hpi=False,
            use_tensorrt=False,
            precision="fp32",
            engine="paddle",
        )
        return cls(cast(_PaddlePipeline, pipeline))

    def ready(self) -> bool:
        """返回本地 pipeline 已完成构造。

        Args:
            无参数。

        Returns:
            引擎对象存在时始终返回 `True`。

        """
        return True

    def recognize(self, image_bytes: bytes) -> OcrEngineResult:
        """识别一张服务层已规范化的 PNG。

        Args:
            image_bytes: 去除元数据并通过像素限制的 PNG。

        Returns:
            按 PaddleOCR 输出顺序排列的文本行。

        Raises:
            ValueError: 图片或 PaddleOCR 输出 schema 无效。

        """
        numpy_module = importlib.import_module("numpy")
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            array = numpy_module.asarray(image.convert("RGB"))
        lines = [
            line
            for result in self._pipeline.predict(array)
            for line in _parse_result(result)
        ]
        return OcrEngineResult(lines=tuple(lines))


def _model_directory(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"OCR 模型路径不是目录：{path.name}")
    required_names = {"inference.json", "inference.pdiparams"}
    names = {item.name for item in resolved.iterdir() if item.is_file()}
    if not required_names.issubset(names):
        raise ValueError(f"OCR 模型目录文件不完整：{path.name}")
    return resolved


def _parse_result(result: object) -> tuple[OcrLine, ...]:
    payload = _result_payload(result)
    texts = payload.get("rec_texts")
    scores = payload.get("rec_scores")
    boxes = payload.get("rec_boxes")
    if (
        not isinstance(texts, Sequence)
        or isinstance(texts, (str, bytes))
        or not isinstance(scores, Sequence)
        or isinstance(scores, (str, bytes))
        or len(texts) != len(scores)
    ):
        raise ValueError("PaddleOCR 输出缺少等长 rec_texts/rec_scores。")
    lines = []
    for index, (text, score) in enumerate(
        zip(texts, scores, strict=True)
    ):
        if not isinstance(text, str) or not text.strip():
            continue
        if not isinstance(score, (float, int)):
            raise ValueError("PaddleOCR 置信度不是数值。")
        confidence = float(score)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("PaddleOCR 置信度超出 [0,1]。")
        lines.append(
            OcrLine(
                text=text.strip(),
                confidence=confidence,
                bbox=_bbox(boxes, index),
            )
        )
    return tuple(lines)


def _result_payload(result: object) -> Mapping[str, object]:
    raw = getattr(result, "json", result)
    if callable(raw):
        raw = raw()
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, Mapping):
        raise ValueError("PaddleOCR 结果不是 JSON 对象。")
    nested = raw.get("res")
    if isinstance(nested, Mapping):
        return cast(Mapping[str, object], nested)
    return cast(Mapping[str, object], raw)


def _bbox(boxes: object, index: int) -> tuple[int, int, int, int]:
    if (
        not isinstance(boxes, Sequence)
        or isinstance(boxes, (str, bytes))
        or index >= len(boxes)
    ):
        return (0, 0, 0, 0)
    box = boxes[index]
    if (
        not isinstance(box, Sequence)
        or isinstance(box, (str, bytes))
        or len(box) != _BBOX_VALUE_COUNT
        or not all(isinstance(value, (float, int)) for value in box)
    ):
        return (0, 0, 0, 0)
    rounded = tuple(round(float(value)) for value in box)
    return cast(tuple[int, int, int, int], rounded)
