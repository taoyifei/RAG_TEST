"""格式解析 adapters。"""

from rag_app.adapters.parsers.docx import DocxOoxmlV4Parser
from rag_app.adapters.parsers.legacy_docx_ir import LegacyDocxIrParser

__all__ = ["DocxOoxmlV4Parser", "LegacyDocxIrParser"]
