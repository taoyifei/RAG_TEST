"""从 Document IR 规划 section、run 和 atomic unit。"""

from __future__ import annotations

import hashlib
from pathlib import PurePath

from rag_app.adapters.chunkers.docx_structural.atoms import (
    AtomicUnit,
    RunPlan,
    SectionPlan,
    SourceFragment,
    node_text_fragments,
)
from rag_app.adapters.chunkers.docx_structural.tables import build_table_run
from rag_app.core.models import (
    ChunkingPolicy,
    ChunkRole,
    DocumentIR,
    DocumentNode,
    NodeKind,
    SourceSpanKind,
    StoryKind,
)
from rag_app.core.models.common import freeze_json_object


def plan_sections(
    document_ir: DocumentIR,
    policy: ChunkingPolicy,
) -> tuple[SectionPlan, ...]:
    """把全部受策略允许的 story 转为禁止跨界的规划。

    Args:
        document_ir: P04 输出的完整格式中立 IR。
        policy: 冻结 ChunkingPolicy。

    Returns:
        按原始 story 和来源顺序排列的 SectionPlan。

    """
    nodes = {node.node_id: node for node in document_ir.nodes}
    note_refs = _note_references(document_ir)
    sections = list(_body_sections(document_ir, nodes, note_refs))
    sections.extend(_note_sections(document_ir, nodes, note_refs))
    sections.extend(_text_box_sections(document_ir, nodes))
    if policy.header_footer_policy == "separate_chunks":
        sections.extend(_furniture_sections(document_ir, nodes))
    if policy.comments_policy == "separate_chunks":
        sections.extend(_comment_sections(document_ir, nodes))
    return tuple(sections)


def _body_sections(
    document_ir: DocumentIR,
    nodes: dict[str, DocumentNode],
    note_refs: dict[str, tuple[str, ...]],
) -> tuple[SectionPlan, ...]:
    candidates = [
        node
        for node in document_ir.nodes
        if node.anchor.story_kind is StoryKind.BODY
        and (
            node.kind in {NodeKind.HEADING, NodeKind.TABLE, NodeKind.IMAGE}
            or (
                node.kind in {NodeKind.PARAGRAPH, NodeKind.LIST_ITEM}
                and not _has_table_ancestor(node, nodes)
                and not _has_text_box_ancestor(node, nodes)
            )
        )
    ]
    ordered = sorted(candidates, key=_node_order)
    heading_path: list[str] = []
    current_section_id = _stable_label(
        "section",
        document_ir.version.document_version_id,
        "root",
    )
    section_entries: list[tuple[str, tuple[str, ...], DocumentNode]] = []
    for node in ordered:
        if node.kind is NodeKind.HEADING:
            level = dict(node.metadata).get("heading_level")
            resolved_level = (
                level if isinstance(level, int) and level > 0 else 1
            )
            del heading_path[resolved_level - 1 :]
            heading_path.append(
                node.text_payload.exact_text if node.text_payload else ""
            )
            current_section_id = _stable_label(
                "section",
                document_ir.version.document_version_id,
                node.node_id,
            )
            continue
        section_entries.append((current_section_id, tuple(heading_path), node))
    grouped: list[SectionPlan] = []
    for section_id, path in _ordered_section_keys(section_entries):
        entries = [
            node
            for item_section, item_path, node in section_entries
            if item_section == section_id and item_path == path
        ]
        runs = _body_runs(document_ir, entries, section_id, path, note_refs)
        if runs:
            grouped.append(
                SectionPlan(
                    section_id=section_id,
                    heading_path=path,
                    runs=runs,
                )
            )
    return tuple(grouped)


def _body_runs(
    document_ir: DocumentIR,
    entries: list[DocumentNode],
    section_id: str,
    heading_path: tuple[str, ...],
    note_refs: dict[str, tuple[str, ...]],
) -> tuple[RunPlan, ...]:
    runs: list[RunPlan] = []
    pending: list[AtomicUnit] = []
    pending_key: tuple[ChunkRole, object, object] | None = None

    def flush() -> None:
        """把当前连续原子冻结为一个 run，并清空暂存状态。

        Args:
            无参数；读取外围函数的暂存原子。

        Returns:
            无返回值；结果追加到外围函数的 runs。

        """
        nonlocal pending, pending_key
        if not pending:
            return
        role = pending[0].role
        group_id = _stable_label(
            "group",
            document_ir.version.document_version_id,
            section_id,
            role.value,
            *(atom.unit_id for atom in pending),
        )
        normalized = tuple(
            AtomicUnit(
                unit_id=atom.unit_id,
                role=atom.role,
                parent_node_id=atom.parent_node_id,
                section_id=atom.section_id,
                neighbor_group_id=group_id,
                heading_path=atom.heading_path,
                fragments=atom.fragments,
                metadata=atom.metadata,
                child_group_ids=atom.child_group_ids,
                note_refs=atom.note_refs,
                table_header_fragments=atom.table_header_fragments,
            )
            for atom in pending
        )
        runs.append(
            RunPlan(
                run_id=_stable_label("run", group_id),
                role=role,
                section_id=section_id,
                neighbor_group_id=group_id,
                heading_path=heading_path,
                atoms=normalized,
            )
        )
        pending = []
        pending_key = None

    for node in entries:
        if node.kind is NodeKind.TABLE:
            flush()
            table_run = build_table_run(
                document_ir,
                node,
                section_id=section_id,
                heading_path=heading_path,
            )
            if table_run is not None:
                runs.append(table_run)
            continue
        if node.kind is NodeKind.IMAGE:
            flush()
            image_atom = _image_atom(node, section_id, heading_path)
            if image_atom is not None:
                runs.append(_single_atom_run(document_ir, image_atom))
            continue
        fragments = node_text_fragments(node)
        if not fragments:
            continue
        role = (
            ChunkRole.LIST
            if node.kind is NodeKind.LIST_ITEM
            else ChunkRole.TEXT
        )
        attributes = node.list_attributes
        key = (
            role,
            dict(node.metadata).get("num_id") if attributes else None,
            attributes.restart_group if attributes else None,
        )
        if pending_key is not None and key != pending_key:
            flush()
        pending_key = key
        pending.append(
            AtomicUnit(
                unit_id=node.node_id,
                role=role,
                parent_node_id=node.node_id,
                section_id=section_id,
                neighbor_group_id="pending",
                heading_path=heading_path,
                fragments=fragments,
                metadata=freeze_json_object(
                    {
                        "story_kind": node.anchor.story_kind.value,
                        "list_attributes": (
                            attributes.model_dump(mode="json")
                            if attributes is not None
                            else None
                        ),
                    }
                ),
                note_refs=note_refs.get(node.node_id, ()),
            )
        )
    flush()
    return tuple(runs)


def _note_sections(
    document_ir: DocumentIR,
    nodes: dict[str, DocumentNode],
    note_refs: dict[str, tuple[str, ...]],
) -> tuple[SectionPlan, ...]:
    referenced = {
        note_id for values in note_refs.values() for note_id in values
    }
    sections: list[SectionPlan] = []
    for note in sorted(
        (node for node in document_ir.nodes if node.kind is NodeKind.NOTE),
        key=_node_order,
    ):
        atoms: list[AtomicUnit] = []
        section_id = _stable_label(
            "section",
            document_ir.version.document_version_id,
            note.node_id,
        )
        group_id = _stable_label("group", section_id, "note")
        for child in _descendants(note, nodes):
            fragments = node_text_fragments(child)
            if not fragments:
                continue
            atoms.append(
                AtomicUnit(
                    unit_id=child.node_id,
                    role=ChunkRole.NOTE,
                    parent_node_id=note.node_id,
                    section_id=section_id,
                    neighbor_group_id=group_id,
                    heading_path=(),
                    fragments=fragments,
                    metadata=(
                        ("note_id", dict(note.metadata).get("note_id")),
                        ("orphan", note.node_id not in referenced),
                    ),
                )
            )
        if atoms:
            sections.append(
                SectionPlan(
                    section_id=section_id,
                    heading_path=(),
                    runs=(
                        RunPlan(
                            run_id=_stable_label("run", group_id),
                            role=ChunkRole.NOTE,
                            section_id=section_id,
                            neighbor_group_id=group_id,
                            heading_path=(),
                            atoms=tuple(atoms),
                        ),
                    ),
                )
            )
    return tuple(sections)


def _text_box_sections(
    document_ir: DocumentIR,
    nodes: dict[str, DocumentNode],
) -> tuple[SectionPlan, ...]:
    return _story_sections(
        document_ir,
        nodes,
        stories=(StoryKind.TEXT_BOX,),
        role=ChunkRole.TEXT_BOX,
    )


def _furniture_sections(
    document_ir: DocumentIR,
    nodes: dict[str, DocumentNode],
) -> tuple[SectionPlan, ...]:
    return _story_sections(
        document_ir,
        nodes,
        stories=(StoryKind.HEADER, StoryKind.FOOTER),
        role=ChunkRole.HEADER_FOOTER,
    )


def _comment_sections(
    document_ir: DocumentIR,
    nodes: dict[str, DocumentNode],
) -> tuple[SectionPlan, ...]:
    return _story_sections(
        document_ir,
        nodes,
        stories=(StoryKind.COMMENT,),
        role=ChunkRole.COMMENT,
    )


def _story_sections(
    document_ir: DocumentIR,
    nodes: dict[str, DocumentNode],
    *,
    stories: tuple[StoryKind, ...],
    role: ChunkRole,
) -> tuple[SectionPlan, ...]:
    roots = [
        node
        for node in document_ir.nodes
        if node.anchor.story_kind in stories
        and (
            node.parent_node_id is None
            or nodes[node.parent_node_id].anchor.story_kind not in stories
        )
    ]
    sections: list[SectionPlan] = []
    for root in sorted(roots, key=_node_order):
        atoms: list[AtomicUnit] = []
        section_id = _stable_label(
            "section",
            document_ir.version.document_version_id,
            root.node_id,
        )
        group_id = _stable_label("group", section_id, role.value)
        candidates = (root, *_descendants(root, nodes))
        for node in candidates:
            fragments = node_text_fragments(node)
            if not fragments:
                continue
            atoms.append(
                AtomicUnit(
                    unit_id=node.node_id,
                    role=role,
                    parent_node_id=root.node_id,
                    section_id=section_id,
                    neighbor_group_id=group_id,
                    heading_path=(),
                    fragments=fragments,
                    metadata=(("story_kind", node.anchor.story_kind.value),),
                )
            )
        if atoms:
            sections.append(
                SectionPlan(
                    section_id=section_id,
                    heading_path=(),
                    runs=(
                        RunPlan(
                            run_id=_stable_label("run", group_id),
                            role=role,
                            section_id=section_id,
                            neighbor_group_id=group_id,
                            heading_path=(),
                            atoms=tuple(atoms),
                        ),
                    ),
                )
            )
    return tuple(sections)


def _image_atom(
    node: DocumentNode,
    section_id: str,
    heading_path: tuple[str, ...],
) -> AtomicUnit | None:
    attributes = node.image_attributes
    if attributes is None:
        return None
    text = attributes.alt_text or attributes.display_name or ""
    if not text or _looks_like_filename_only(text):
        return None
    fragment = SourceFragment(
        text=text,
        span_type=SourceSpanKind.ORIGINAL_TEXT,
        node_id=node.node_id,
        source_anchor=node.anchor,
        source_start_char=0,
        source_end_char=len(text),
        metadata=(("source_field", "alt_or_caption"),),
    )
    group_id = _stable_label("group", section_id, node.node_id)
    return AtomicUnit(
        unit_id=node.node_id,
        role=ChunkRole.IMAGE_METADATA,
        parent_node_id=node.node_id,
        section_id=section_id,
        neighbor_group_id=group_id,
        heading_path=heading_path,
        fragments=(fragment,),
        metadata=(
            ("blob_ref", attributes.blob_ref),
            ("media_type", attributes.media_type),
            ("ocr_state", "disabled"),
        ),
    )


def _single_atom_run(
    document_ir: DocumentIR,
    atom: AtomicUnit,
) -> RunPlan:
    del document_ir
    return RunPlan(
        run_id=_stable_label("run", atom.neighbor_group_id),
        role=atom.role,
        section_id=atom.section_id,
        neighbor_group_id=atom.neighbor_group_id,
        heading_path=atom.heading_path,
        atoms=(atom,),
    )


def _note_references(document_ir: DocumentIR) -> dict[str, tuple[str, ...]]:
    collected: dict[str, list[str]] = {}
    for relationship in document_ir.relationships:
        if relationship.relationship_type not in {
            "document-footnote",
            "document-endnote",
        }:
            continue
        collected.setdefault(relationship.source_node_id, []).append(
            relationship.target_node_id
        )
    return {
        key: tuple(dict.fromkeys(values)) for key, values in collected.items()
    }


def _descendants(
    root: DocumentNode,
    nodes: dict[str, DocumentNode],
) -> tuple[DocumentNode, ...]:
    pending = list(root.child_ids)
    descendants: list[DocumentNode] = []
    while pending:
        node = nodes[pending.pop(0)]
        descendants.append(node)
        pending[0:0] = list(node.child_ids)
    return tuple(descendants)


def _has_table_ancestor(
    node: DocumentNode,
    nodes: dict[str, DocumentNode],
) -> bool:
    parent_id = node.parent_node_id
    while parent_id is not None:
        parent = nodes[parent_id]
        if parent.kind is NodeKind.TABLE:
            return True
        parent_id = parent.parent_node_id
    return False


def _has_text_box_ancestor(
    node: DocumentNode,
    nodes: dict[str, DocumentNode],
) -> bool:
    parent_id = node.parent_node_id
    while parent_id is not None:
        parent = nodes[parent_id]
        if parent.anchor.story_kind is StoryKind.TEXT_BOX:
            return True
        parent_id = parent.parent_node_id
    return False


def _ordered_section_keys(
    entries: list[tuple[str, tuple[str, ...], DocumentNode]],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    keys: list[tuple[str, tuple[str, ...]]] = []
    for section_id, path, _ in entries:
        key = (section_id, path)
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def _node_order(node: DocumentNode) -> tuple[int, int, str]:
    return (node.anchor.ordinal, node.order, node.node_id)


def _looks_like_filename_only(text: str) -> bool:
    path = PurePath(text)
    return bool(path.suffix and path.stem and " " not in text)


def _stable_label(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"
