"""格式解析 adapters。"""

from rag_app.adapters.parsers.docx import DocxOoxmlV4Parser
from rag_app.adapters.parsers.legacy_docx_ir import LegacyDocxIrParser
from rag_app.adapters.parsers.word_document import WordDocumentV1Parser

__all__ = [
    "DocxOoxmlV4Parser",
    "LegacyDocxIrParser",
    "WordDocumentV1Parser",
]
