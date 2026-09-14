"""PaddleOCR 双路径 PDF Parser Adapter。"""

from rag_app.adapters.parsers.pdf.critical_ocr import (
    PaddleOfficialCriticalOcr,
    PaddleSelfHostedCriticalOcr,
    recognized_contains,
    render_pdf_region,
)
from rag_app.adapters.parsers.pdf.document_parser import PdfIrParser
from rag_app.adapters.parsers.pdf.official import (
    PaddleOfficialApiPdfConfig,
    PaddleOfficialApiPdfParser,
)
from rag_app.adapters.parsers.pdf.self_hosted import (
    PaddleSelfHostedPdfConfig,
    PaddleSelfHostedPdfParser,
)
from rag_app.adapters.parsers.pdf.source import inspect_pdf_source

__all__ = [
    "PaddleOfficialApiPdfConfig",
    "PaddleOfficialApiPdfParser",
    "PaddleOfficialCriticalOcr",
    "PaddleSelfHostedCriticalOcr",
    "PaddleSelfHostedPdfConfig",
    "PaddleSelfHostedPdfParser",
    "PdfIrParser",
    "inspect_pdf_source",
    "recognized_contains",
    "render_pdf_region",
]
