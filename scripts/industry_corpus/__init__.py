"""工业旧版 Word 语料的离线预处理组件。"""

from scripts.industry_corpus.ooxml import (
    HeadingDecision,
    OoxmlAudit,
    clean_docx,
    heading_candidate,
)
from scripts.industry_corpus.workflow import (
    EXPECTED_INVENTORY,
    CorpusPreparationError,
    PreparedCorpus,
    SourceSpec,
    prepare_industry_corpus,
)

__all__ = [
    "EXPECTED_INVENTORY",
    "CorpusPreparationError",
    "HeadingDecision",
    "OoxmlAudit",
    "PreparedCorpus",
    "SourceSpec",
    "clean_docx",
    "heading_candidate",
    "prepare_industry_corpus",
]
