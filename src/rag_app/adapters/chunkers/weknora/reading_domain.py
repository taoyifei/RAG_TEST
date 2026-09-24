"""为固定上游分块器准备保真阅读域和可追踪来源。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from rag_app.adapters.chunkers.docx_structural.atoms import (
    AtomicUnit,
    RunPlan,
    SourceFragment,
    node_text_fragments,
)
from rag_app.adapters.chunkers.docx_structural.rendering import (
    render_fragments,
    separator_fragment,
)
from rag_app.adapters.chunkers.docx_structural.sections import plan_sections
from rag_app.adapters.chunkers.docx_structural.tables import build_table_run
from rag_app.adapters.chunkers.weknora.reading_view import (
    ReadingView,
    build_reading_view,
    normalize_reading_view,
)
from rag_app.core.models import (
    ChunkContextDependency,
    ChunkRole,
    DocumentIR,
    DocumentNode,
    NodeKind,
    StoryKind,
    WeKnoraChunkingPolicy,
)

_AUXILIARY_ROLES = frozenset(
    {
        ChunkRole.NOTE,
        ChunkRole.IMAGE_METADATA,
        ChunkRole.HEADER_FOOTER,
        ChunkRole.TEXT_BOX,
        ChunkRole.COMMENT,
    }
)
_MAX_HEADING_LEVEL = 6


@dataclass(frozen=True, slots=True)
class ReadingDomain:
    """一次 Go 输入及其不可跨越的来源边界。"""

    domain_id: str
    role: ChunkRole
    section_id: str
    neighbor_group_id: str
    heading_path: tuple[str, ...]
    context_dependencies: tuple[ChunkContextDependency, ...]
    atoms: tuple[AtomicUnit, ...]
    view: ReadingView
    boundary: str


def stable_domain_label(prefix: str, *parts: object) -> str:
    """生成阅读域内部身份，不混用公共实体 ID 前缀。"""
    digest = hashlib.sha256(
        "\x1f".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()
    return f"{prefix}_{digest[:32]}"


def plan_reading_domains(
    document_ir: DocumentIR,
    policy: WeKnoraChunkingPolicy,
) -> tuple[ReadingDomain, ...]:
    """让候选分块读取连续正文，仅在真实来源边界处隔离。"""
    if dict(document_ir.metadata).get("reading_domain_revision") == (
        "weknora-markdown-domain-v2"
    ):
        return (_parsed_markdown_domain(document_ir),)
    if document_ir.source.extension != ".docx":
        return _legacy_domains(document_ir, policy)
    body = _docx_body_domains(document_ir)
    auxiliary = tuple(
        domain
        for domain in _legacy_domains(document_ir, policy)
        if domain.role in _AUXILIARY_ROLES
    )
    return (*body, *auxiliary)


def _parsed_markdown_domain(document_ir: DocumentIR) -> ReadingDomain:
    nodes = sorted(
        document_ir.nodes,
        key=_parsed_start,
    )
    artifact_id = dict(document_ir.metadata).get("parsed_artifact_id")
    fragments: list[SourceFragment] = []
    atoms: list[AtomicUnit] = []
    cursor = 0
    domain_id = stable_domain_label(
        "run", document_ir.version.document_version_id, "markdown-domain-v2"
    )
    section_id = stable_domain_label("section", domain_id)
    group_id = stable_domain_label("group", domain_id)
    for node in nodes:
        metadata = dict(node.metadata)
        text = node.text_payload.exact_text if node.text_payload else ""
        start = metadata.get("parsed_artifact_start_char")
        end = metadata.get("parsed_artifact_end_char")
        if (
            metadata.get("parsed_artifact_id") != artifact_id
            or type(start) is not int
            or type(end) is not int
            or start != cursor
            or end != start + len(text)
            or node.anchor.story_kind is not StoryKind.BODY
        ):
            raise ValueError("解析阅读域的来源区间不连续或身份不一致。")
        cursor = end
        if not text.strip():
            fragments.append(separator_fragment(text))
            continue
        node_fragments = node_text_fragments(node)
        fragments.extend(node_fragments)
        syntax = metadata.get("reading_syntax")
        role = (
            ChunkRole.TABLE
            if syntax == "table"
            else ChunkRole.LIST
            if syntax == "list"
            else ChunkRole.TEXT
        )
        atoms.append(
            AtomicUnit(
                unit_id=node.node_id,
                role=role,
                parent_node_id=node.node_id,
                section_id=section_id,
                neighbor_group_id=group_id,
                heading_path=(),
                fragments=node_fragments,
            )
        )
    rendered = render_fragments(tuple(fragments))
    if not rendered.text.strip() or not atoms:
        raise ValueError("解析阅读域没有可索引正文。")
    return ReadingDomain(
        domain_id=domain_id,
        role=ChunkRole.TEXT,
        section_id=section_id,
        neighbor_group_id=group_id,
        heading_path=(),
        context_dependencies=(),
        atoms=tuple(atoms),
        view=normalize_reading_view(ReadingView(rendered.text, rendered.spans)),
        boundary="parsed_artifact_body",
    )


def _docx_body_domains(document_ir: DocumentIR) -> tuple[ReadingDomain, ...]:
    nodes = {node.node_id: node for node in document_ir.nodes}
    candidates = sorted(
        (
            node
            for node in document_ir.nodes
            if node.anchor.story_kind is StoryKind.BODY
            and node.kind
            in {
                NodeKind.HEADING,
                NodeKind.PARAGRAPH,
                NodeKind.LIST_ITEM,
                NodeKind.TABLE,
            }
            and not _has_ancestor(node, nodes, NodeKind.TABLE)
            and not _has_story_ancestor(node, nodes, StoryKind.TEXT_BOX)
        ),
        key=lambda node: (node.anchor.ordinal, node.order, node.node_id),
    )
    domains: list[ReadingDomain] = []
    pending: list[AtomicUnit] = []
    boundary: tuple[str, str, int | None] | None = None

    def flush() -> None:
        if pending:
            domains.append(
                _domain_from_atoms(
                    document_ir,
                    tuple(pending),
                    boundary_label="docx_body",
                )
            )
            pending.clear()

    for node in candidates:
        current = (
            node.anchor.part_uri,
            node.anchor.story_kind.value,
            node.anchor.section_index,
        )
        if current != boundary:
            flush()
            boundary = current
        if node.kind is NodeKind.TABLE:
            flush()
            section_id = stable_domain_label(
                "section", document_ir.version.document_version_id, node.node_id
            )
            run = build_table_run(
                document_ir,
                node,
                section_id=section_id,
                heading_path=(),
            )
            if run is not None:
                domains.append(_domain_from_run(run, nodes, "docx_table"))
            continue
        fragments = node_text_fragments(node)
        if not fragments:
            continue
        if node.kind is NodeKind.HEADING:
            level = dict(node.metadata).get("heading_level")
            level = (
                level
                if isinstance(level, int) and 1 <= level <= _MAX_HEADING_LEVEL
                else 1
            )
            fragments = (separator_fragment("#" * level + " "), *fragments)
        pending.append(
            AtomicUnit(
                unit_id=node.node_id,
                role=(
                    ChunkRole.LIST
                    if node.kind is NodeKind.LIST_ITEM
                    else ChunkRole.TEXT
                ),
                parent_node_id=node.node_id,
                section_id="pending",
                neighbor_group_id="pending",
                heading_path=(),
                fragments=fragments,
                note_refs=_note_refs(document_ir, node.node_id),
            )
        )
    flush()
    return tuple(domains)


def _domain_from_atoms(
    document_ir: DocumentIR,
    atoms: tuple[AtomicUnit, ...],
    *,
    boundary_label: str,
) -> ReadingDomain:
    domain_id = stable_domain_label(
        "run",
        document_ir.version.document_version_id,
        boundary_label,
        *(atom.unit_id for atom in atoms),
    )
    section_id = stable_domain_label("section", domain_id)
    group_id = stable_domain_label("group", domain_id)
    normalized_atoms = tuple(
        AtomicUnit(
            unit_id=atom.unit_id,
            role=atom.role,
            parent_node_id=atom.parent_node_id,
            section_id=section_id,
            neighbor_group_id=group_id,
            heading_path=atom.heading_path,
            fragments=atom.fragments,
            metadata=atom.metadata,
            child_group_ids=atom.child_group_ids,
            note_refs=atom.note_refs,
            table_header_fragments=atom.table_header_fragments,
            structural_context=atom.structural_context,
            context_dependencies=atom.context_dependencies,
        )
        for atom in atoms
    )
    run = RunPlan(
        run_id=domain_id,
        role=ChunkRole.TEXT,
        section_id=section_id,
        neighbor_group_id=group_id,
        heading_path=(),
        atoms=normalized_atoms,
    )
    return _domain_from_run(
        run,
        {node.node_id: node for node in document_ir.nodes},
        boundary_label,
    )


def _domain_from_run(
    run: RunPlan,
    nodes: dict[str, DocumentNode],
    boundary: str,
) -> ReadingDomain:
    return ReadingDomain(
        domain_id=run.run_id,
        role=run.role,
        section_id=run.section_id,
        neighbor_group_id=run.neighbor_group_id,
        heading_path=run.heading_path,
        context_dependencies=run.context_dependencies,
        atoms=run.atoms,
        view=build_reading_view(run, nodes),
        boundary=boundary,
    )


def _legacy_domains(
    document_ir: DocumentIR,
    policy: WeKnoraChunkingPolicy,
) -> tuple[ReadingDomain, ...]:
    nodes = {node.node_id: node for node in document_ir.nodes}
    return tuple(
        _domain_from_run(run, nodes, "legacy_isolated_story")
        for section in plan_sections(document_ir, policy)
        for run in section.runs
    )


def _has_ancestor(
    node: DocumentNode,
    nodes: dict[str, DocumentNode],
    kind: NodeKind,
) -> bool:
    parent_id = node.parent_node_id
    while parent_id is not None:
        parent = nodes[parent_id]
        if parent.kind is kind:
            return True
        parent_id = parent.parent_node_id
    return False


def _has_story_ancestor(
    node: DocumentNode,
    nodes: dict[str, DocumentNode],
    story: StoryKind,
) -> bool:
    parent_id = node.parent_node_id
    while parent_id is not None:
        parent = nodes[parent_id]
        if parent.anchor.story_kind is story:
            return True
        parent_id = parent.parent_node_id
    return False


def _note_refs(document_ir: DocumentIR, node_id: str) -> tuple[str, ...]:
    return tuple(
        relationship.target_node_id
        for relationship in document_ir.relationships
        if relationship.source_node_id == node_id
        and relationship.relationship_type
        in {"document-footnote", "document-endnote"}
    )


def _parsed_start(node: DocumentNode) -> int:
    value = dict(node.metadata).get("parsed_artifact_start_char")
    return value if type(value) is int else -1
