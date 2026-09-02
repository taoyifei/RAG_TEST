"""把 adapter 内部节点草稿组装成格式中立 Document IR 节点。"""

from __future__ import annotations

import hashlib

from rag_app.adapters.parsers.docx.models import NodeDraft, json_value
from rag_app.core.identifiers import node_id
from rag_app.core.models import (
    CellGrid,
    DocumentNode,
    ImageAttributes,
    ListAttributes,
    NodeKind,
    RevisionMark,
    SourceAnchor,
    text_payload,
)

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class IrNodeBuilder:
    """维护父子顺序和确定性 node ID 的内部 builder。"""

    def __init__(self, document_version_id: str) -> None:
        """创建空节点表。

        Args:
            document_version_id: 绑定输入内容的稳定版本 ID。

        Returns:
            无返回值。

        """
        self._document_version_id = document_version_id
        self._drafts: list[NodeDraft] = []
        self._by_id: dict[str, NodeDraft] = {}
        self._root_ids: list[str] = []

    @property
    def drafts(self) -> tuple[NodeDraft, ...]:
        """返回按创建顺序排列的草稿快照。

        Args:
            无参数；读取当前 builder。

        Returns:
            节点草稿的只读元组。

        """
        return tuple(self._drafts)

    @property
    def root_ids(self) -> tuple[str, ...]:
        """返回按文档顺序排列的根节点 ID。

        Args:
            无参数；读取当前 builder。

        Returns:
            根节点 ID 元组。

        """
        return tuple(self._root_ids)

    def get(self, generated_node_id: str) -> NodeDraft:
        """读取一个可由表格合并逻辑更新的草稿。

        Args:
            generated_node_id: 已生成的节点 ID。

        Returns:
            对应的 adapter 内部草稿。

        """
        return self._by_id[generated_node_id]

    def add(  # noqa: PLR0913
        self,
        *,
        kind: NodeKind,
        anchor: SourceAnchor,
        parent_node_id: str | None = None,
        exact_text: str | None = None,
        semantic_text: str | None = None,
        revision_mark: RevisionMark | None = None,
        list_attributes: ListAttributes | None = None,
        cell_grid: CellGrid | None = None,
        image_attributes: ImageAttributes | None = None,
        metadata: dict[str, object] | None = None,
    ) -> NodeDraft:
        """添加一个节点并同步父节点 child 顺序。

        Args:
            kind: 格式中立节点类型。
            anchor: 稳定来源锚点。
            parent_node_id: 可选父节点。
            exact_text: 可选精确显示文本。
            semantic_text: 可选最小规范化文本。
            revision_mark: 可选修订标记。
            list_attributes: 可选列表属性。
            cell_grid: 可选表格网格。
            image_attributes: 可选图片 Blob 属性。
            metadata: 可选安全 JSON 元数据。

        Returns:
            新建且可供内部结构更新的节点草稿。

        """
        payload = (
            None
            if exact_text is None
            else text_payload(exact_text, semantic_text)
        )
        content_sha256 = (
            payload.semantic_sha256
            if payload is not None
            else image_attributes.content_sha256
            if image_attributes is not None
            else _EMPTY_SHA256
        )
        generated_node_id = node_id(
            self._document_version_id,
            anchor.part_uri,
            anchor.structural_path,
            kind.value,
            content_sha256,
        )
        if generated_node_id in self._by_id:
            raise ValueError("DOCX 节点结构路径与内容组合必须唯一。")
        siblings = (
            self._root_ids
            if parent_node_id is None
            else self._by_id[parent_node_id].children
        )
        draft = NodeDraft(
            node_id=generated_node_id,
            kind=kind,
            parent_node_id=parent_node_id,
            order=len(siblings),
            anchor=anchor,
            text_payload=payload,
            revision_mark=revision_mark,
            list_attributes=list_attributes,
            cell_grid=cell_grid,
            image_attributes=image_attributes,
            metadata=dict(metadata or {}),
        )
        siblings.append(generated_node_id)
        self._drafts.append(draft)
        self._by_id[generated_node_id] = draft
        return draft

    def freeze(self) -> tuple[DocumentNode, ...]:
        """生成通过 Core 单节点校验的不可变节点表。

        Args:
            无参数。

        Returns:
            保留创建顺序的 DocumentNode 元组。

        """
        return tuple(
            DocumentNode(
                node_id=draft.node_id,
                kind=draft.kind,
                parent_node_id=draft.parent_node_id,
                child_ids=tuple(draft.children),
                order=draft.order,
                anchor=draft.anchor,
                text_payload=draft.text_payload,
                revision_mark=draft.revision_mark,
                list_attributes=draft.list_attributes,
                cell_grid=draft.cell_grid,
                image_attributes=draft.image_attributes,
                metadata=tuple(
                    sorted(
                        (
                            key,
                            json_value(value),
                        )
                        for key, value in draft.metadata.items()
                    )
                ),
            )
            for draft in self._drafts
        )
