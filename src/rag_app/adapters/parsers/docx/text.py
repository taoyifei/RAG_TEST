"""按 Word 显示顺序提取段落文本、字段和引用标记。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from lxml import etree

from rag_app.adapters.parsers.docx.drawings import image_references
from rag_app.adapters.parsers.docx.issues import IssueCollector
from rag_app.adapters.parsers.docx.models import TextExtraction
from rag_app.adapters.parsers.docx.namespaces import (
    OFFICE_REL,
    WORD,
    local_name,
    qn,
    word_attr,
)
from rag_app.adapters.parsers.docx.revisions import revision_visibility
from rag_app.core.errors import InvalidDocument
from rag_app.core.models import RevisionMark
from rag_app.core.policies import (
    HiddenTextPolicy,
    ParsingPolicy,
    TrackedChangesPolicy,
)

_MIN_PRINTABLE_CODEPOINT = 0x20
_MAX_UNICODE_CODEPOINT = 0x10FFFF


@dataclass(slots=True)
class _FieldState:
    instruction_parts: list[str] = field(default_factory=list)
    has_separator: bool = False
    has_result: bool = False


@dataclass(slots=True)
class _TextState:
    exact_parts: list[str] = field(default_factory=list)
    semantic_parts: list[str] = field(default_factory=list)
    fields: list[_FieldState] = field(default_factory=list)
    field_types: list[str] = field(default_factory=list)
    field_hashes: list[str] = field(default_factory=list)
    field_targets: list[str] = field(default_factory=list)
    break_types: list[str] = field(default_factory=list)
    note_references: list[tuple[str, str]] = field(default_factory=list)
    comment_references: list[str] = field(default_factory=list)
    comment_range_starts: list[str] = field(default_factory=list)
    comment_range_ends: list[str] = field(default_factory=list)
    bookmark_names: list[str] = field(default_factory=list)
    hyperlink_kinds: list[str] = field(default_factory=list)
    hyperlink_relationship_ids: list[str] = field(default_factory=list)
    hyperlink_anchors: list[str] = field(default_factory=list)
    revision_count: int = 0
    revision_mark: RevisionMark | None = None
    deleted_characters: int = 0
    hidden_runs: int = 0
    is_toc: bool = False


def extract_paragraph_text(
    paragraph: etree._Element,
    policy: ParsingPolicy,
    issues: IssueCollector,
) -> TextExtraction:
    """解析一个段落的可见字符和安全结构元数据。

    Args:
        paragraph: WordprocessingML 段落。
        policy: 修订、隐藏文字和 soft hyphen 策略。
        issues: 共享问题收集器。

    Returns:
        精确文本、语义文本、字段和引用信息。

    Raises:
        InvalidDocument: 策略拒绝修订或隐藏文字。

    """
    state = _TextState()
    _walk(paragraph, policy, issues, state, hidden=False, deleted=False)
    if state.fields:
        issues.add(
            "DOCX_FIELD_UNCLOSED",
            action="use_visible_result",
            message="复杂字段缺少结束标记。",
            count=len(state.fields),
        )
    references = image_references(paragraph)
    metadata: list[tuple[str, object]] = []
    if state.field_types:
        metadata.append(("field_types", tuple(state.field_types)))
        metadata.append(("field_instruction_hashes", tuple(state.field_hashes)))
    if state.hyperlink_kinds:
        metadata.append(("hyperlink_kinds", tuple(state.hyperlink_kinds)))
    if state.hyperlink_relationship_ids:
        metadata.append(
            (
                "hyperlink_relationship_ids",
                tuple(state.hyperlink_relationship_ids),
            )
        )
    if state.hyperlink_anchors:
        metadata.append(("hyperlink_anchors", tuple(state.hyperlink_anchors)))
    if state.field_targets:
        metadata.append(("field_targets", tuple(state.field_targets)))
    if state.deleted_characters:
        metadata.append(("deleted_characters", state.deleted_characters))
    if state.hidden_runs:
        metadata.append(("hidden_runs", state.hidden_runs))
    if state.revision_count:
        metadata.append(("revision_count", state.revision_count))
    if state.comment_range_starts:
        metadata.append(
            ("comment_range_starts", tuple(state.comment_range_starts))
        )
    if state.comment_range_ends:
        metadata.append(
            ("comment_range_ends", tuple(state.comment_range_ends))
        )
    return TextExtraction(
        exact_text="".join(state.exact_parts),
        semantic_text="".join(state.semantic_parts),
        metadata=tuple(metadata),
        revision_mark=state.revision_mark,
        break_types=tuple(state.break_types),
        image_relationship_ids=tuple(
            reference.relationship_id for reference in references
        ),
        note_references=tuple(state.note_references),
        comment_references=tuple(state.comment_references),
        bookmark_names=tuple(state.bookmark_names),
        is_toc=state.is_toc,
    )


def _walk(  # noqa: PLR0911, PLR0912, PLR0913, PLR0915
    node: etree._Element,
    policy: ParsingPolicy,
    issues: IssueCollector,
    state: _TextState,
    *,
    hidden: bool,
    deleted: bool,
) -> None:
    kind = local_name(node)
    if kind == "txbxContent":
        return
    if kind in {"ins", "del", "moveFrom", "moveTo"}:
        visible, mark = revision_visibility(node, policy)
        state.revision_count += 1
        revision_deleted = kind in {"del", "moveFrom"}
        if not visible:
            if policy.tracked_changes is TrackedChangesPolicy.ALL_WITH_MARKERS:
                state.deleted_characters += sum(
                    len(item.text or "")
                    for item in node.iter()
                    if local_name(item) in {"t", "delText"}
                )
            return
        state.revision_mark = state.revision_mark or mark
        for child in node:
            _walk(
                child,
                policy,
                issues,
                state,
                hidden=hidden,
                deleted=deleted or revision_deleted,
            )
        return
    if kind == "r":
        run_hidden = _run_hidden(node)
        if run_hidden:
            state.hidden_runs += 1
            if policy.hidden_text is HiddenTextPolicy.REJECT:
                raise InvalidDocument(
                    "ParsingPolicy 拒绝包含隐藏文字的 DOCX。",
                    stage="docx-ooxml-v4.hidden-text",
                )
            if policy.hidden_text is HiddenTextPolicy.EXCLUDE:
                issues.add(
                    "DOCX_HIDDEN_TEXT_EXCLUDED",
                    action="exclude",
                    message="隐藏文字未进入正文。",
                )
                return
        for child in node:
            if local_name(child) == "rPr":
                continue
            _walk(
                child,
                policy,
                issues,
                state,
                hidden=hidden or run_hidden,
                deleted=deleted,
            )
        return
    if kind == "fldSimple":
        instruction = node.get(word_attr("instr")) or ""
        _record_instruction(instruction, state)
        for child in node:
            _walk(
                child,
                policy,
                issues,
                state,
                hidden=hidden,
                deleted=deleted,
            )
        return
    if kind == "fldChar":
        _field_character(node, policy, issues, state)
        return
    if kind == "instrText":
        if state.fields:
            state.fields[-1].instruction_parts.append(node.text or "")
        return
    if kind in {"t", "delText"}:
        value = node.text or ""
        if deleted:
            state.deleted_characters += len(value)
            return
        if state.fields and not state.fields[-1].has_separator:
            return
        if state.fields:
            state.fields[-1].has_result = True
        _append_text(value, value, state)
        return
    if kind == "tab":
        _append_text("\t", "\t", state)
        return
    if kind in {"br", "cr"}:
        break_type = node.get(word_attr("type")) or "line"
        state.break_types.append(break_type)
        if break_type == "line":
            _append_text("\n", "\n", state)
        return
    if kind == "noBreakHyphen":
        _append_text("‑", "‑", state)
        return
    if kind == "softHyphen":
        semantic = "\u00ad" if policy.preserve_soft_hyphen else ""
        _append_text("\u00ad", semantic, state)
        return
    if kind == "sym":
        symbol = _symbol_character(node)
        if symbol is not None:
            _append_text(symbol, symbol, state)
        else:
            issues.add(
                "DOCX_SYMBOL_UNMAPPED",
                action="metadata_only",
                message="字体符号无法可靠映射为 Unicode。",
            )
        return
    if kind in {"footnoteReference", "endnoteReference"}:
        reference_id = node.get(word_attr("id"))
        if reference_id is not None:
            state.note_references.append((kind, reference_id))
        return
    if kind == "commentReference":
        reference_id = node.get(word_attr("id"))
        if reference_id is not None:
            state.comment_references.append(reference_id)
        return
    if kind in {"commentRangeStart", "commentRangeEnd"}:
        reference_id = node.get(word_attr("id"))
        if reference_id is not None:
            target = (
                state.comment_range_starts
                if kind == "commentRangeStart"
                else state.comment_range_ends
            )
            target.append(reference_id)
        return
    if kind == "bookmarkStart":
        name = node.get(word_attr("name"))
        if name:
            state.bookmark_names.append(name)
        return
    if kind == "hyperlink":
        relationship_id = node.get(qn(OFFICE_REL, "id"))
        anchor = node.get(word_attr("anchor"))
        if relationship_id:
            state.hyperlink_kinds.append("external_or_relationship")
            state.hyperlink_relationship_ids.append(relationship_id)
        elif anchor:
            state.hyperlink_kinds.append("internal_anchor")
            state.hyperlink_anchors.append(anchor)
    for child in node:
        _walk(
            child,
            policy,
            issues,
            state,
            hidden=hidden,
            deleted=deleted,
        )


def _append_text(exact: str, semantic: str, state: _TextState) -> None:
    if state.fields and not state.fields[-1].has_separator:
        return
    state.exact_parts.append(exact)
    state.semantic_parts.append(semantic)


def _field_character(
    node: etree._Element,
    policy: ParsingPolicy,
    issues: IssueCollector,
    state: _TextState,
) -> None:
    field_type = node.get(word_attr("fldCharType")) or ""
    if field_type == "begin":
        if len(state.fields) >= policy.max_field_depth:
            raise InvalidDocument(
                "DOCX 嵌套字段深度超过限制。",
                stage="docx-ooxml-v4.resource",
            )
        state.fields.append(_FieldState())
        return
    if not state.fields:
        issues.add(
            "DOCX_FIELD_STATE_INVALID",
            action="ignore_marker",
            message="字段状态标记缺少 begin。",
        )
        return
    current = state.fields[-1]
    if field_type == "separate":
        current.has_separator = True
        _record_instruction("".join(current.instruction_parts), state)
        return
    if field_type == "end":
        completed = state.fields.pop()
        if not completed.has_separator or not completed.has_result:
            issues.add(
                "DOCX_FIELD_RESULT_MISSING",
                action="metadata_only",
                message="字段没有可见结果。",
            )


def _record_instruction(instruction: str, state: _TextState) -> None:
    normalized = " ".join(instruction.strip().split())
    if not normalized:
        return
    field_type = normalized.split(maxsplit=1)[0].upper()[:64]
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    state.field_types.append(field_type)
    state.field_hashes.append(digest)
    if field_type in {"REF", "PAGEREF"}:
        parts = normalized.split()
        if len(parts) > 1:
            state.field_targets.append(parts[1][:128])
    if field_type == "TOC":
        state.is_toc = True


def _run_hidden(run: etree._Element) -> bool:
    properties = run.find(qn(WORD, "rPr"))
    if properties is None:
        return False
    vanish = properties.find(qn(WORD, "vanish"))
    if vanish is None:
        return False
    value = vanish.get(word_attr("val"))
    return value is None or value.casefold() not in {"0", "false", "off"}


def _symbol_character(node: etree._Element) -> str | None:
    value = node.get(word_attr("char"))
    if value is None:
        return None
    try:
        codepoint = int(value, 16)
    except ValueError:
        return None
    if _MIN_PRINTABLE_CODEPOINT <= codepoint <= _MAX_UNICODE_CODEPOINT:
        try:
            return chr(codepoint)
        except ValueError:
            return None
    return None
