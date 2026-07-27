"""受支持的文档解析器。"""

from rag_app.parsers.docx import DocxParser, DocxParserLimits, UnsafeDocxError

__all__ = ["DocxParser", "DocxParserLimits", "UnsafeDocxError"]
