"""只允许模型读取本次候选后生成，并执行 citation validation。"""

from rag_app.application.answering.grounded import (
    GroundedAnsweringService,
    GroundedOutcome,
    validate_grounded_draft,
)
from rag_app.application.answering.validation import validate_extractive_draft

__all__ = [
    "GroundedAnsweringService",
    "GroundedOutcome",
    "validate_extractive_draft",
    "validate_grounded_draft",
]
