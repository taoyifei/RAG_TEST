"""图关系候选、人工发布状态与可检索证据的产品合同。"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, StrictInt, field_validator, model_validator

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.errors import NotFound, PolicyDenied
from rag_app.core.identifiers import (
    canonical_json,
    canonical_sha256,
    deterministic_id,
)
from rag_app.core.models import DocumentIR, DocumentNode, NodeKind, ParseResult
from rag_app.core.models.common import FrozenModel, freeze_json_object
from rag_app.core.models.document import ParseIssue, text_payload

_POLICY_VERSION = "diagram-relation-publication-v1"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class RelationDirection(StrEnum):
    """候选边相对于 source/target 的方向。"""

    SOURCE_TO_TARGET = "source_to_target"
    TARGET_TO_SOURCE = "target_to_source"
    BIDIRECTIONAL = "bidirectional"
    UNKNOWN = "unknown"


class RelationEvidenceSource(StrEnum):
    """关系结论的真实来源，不把视觉推断伪装成原生结构。"""

    NATIVE_DRAWING = "native_drawing"
    VISUAL_CANDIDATE = "visual_candidate"
    SYNTHETIC_CONTRACT = "synthetic_contract"


class RelationReviewState(StrEnum):
    """候选是否具备进入正式回答证据的资格。"""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


class DiagramRelationNode(FrozenModel):
    """一张图内由 Provider 或原生连接器定位的逻辑节点。"""

    node_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    label: str = Field(min_length=1, max_length=256)
    bbox: tuple[float, float, float, float] | None = None

    @field_validator("bbox")
    @classmethod
    def _validate_bbox(
        cls, value: tuple[float, float, float, float] | None
    ) -> tuple[float, float, float, float] | None:
        if value is None:
            return None
        left, top, right, bottom = value
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError("关系节点 bbox 必须是 0..1 内前进的归一化坐标。")
        return value


class DiagramRelationCandidate(FrozenModel):
    """绑定图像 occurrence、模型和政策版本的结构化关系候选。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    candidate_id: str = Field(pattern=r"^drel_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    figure_occurrence_id: str = Field(pattern=r"^node_[0-9a-f]{32}$")
    media_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_node: DiagramRelationNode
    target_node: DiagramRelationNode
    relation_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    direction: RelationDirection
    evidence_source: RelationEvidenceSource
    model_id: str = Field(min_length=1, max_length=256)
    policy_version: str = Field(min_length=1, max_length=128)
    ambiguous: bool = False
    review_state: RelationReviewState = RelationReviewState.PENDING
    review_revision: StrictInt = Field(default=0, ge=0)
    created_at: datetime
    reviewed_at: datetime | None = None
    reviewer_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @field_validator("created_at", "reviewed_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("图关系时间必须包含时区。")
        return value

    @model_validator(mode="after")
    def _validate_publication_state(self) -> DiagramRelationCandidate:
        if self.source_node.node_id == self.target_node.node_id:
            raise ValueError("关系 source/target 不能相同。")
        if self.ambiguous and self.review_state is RelationReviewState.ACCEPTED:
            raise ValueError("有歧义的图关系不得发布为正式证据。")
        if (
            self.direction is RelationDirection.UNKNOWN
            and self.review_state is RelationReviewState.ACCEPTED
        ):
            raise ValueError("方向未知的图关系不得发布为正式证据。")
        if self.review_state is RelationReviewState.PENDING:
            if self.reviewed_at is not None or self.reviewer_sha256 is not None:
                raise ValueError("待审候选不能伪造审阅身份或时间。")
        elif self.reviewed_at is None:
            raise ValueError("已审候选必须保存审阅时间。")
        return self


def relation_candidate_id(  # noqa: PLR0913
    *,
    document_version_id: str,
    figure_occurrence_id: str,
    media_sha256: str,
    source_node: DiagramRelationNode,
    target_node: DiagramRelationNode,
    relation_type: str,
    direction: RelationDirection,
    model_id: str,
    policy_version: str,
) -> str:
    """根据完整候选身份生成稳定但不暴露正文的 ID。

    Args:
        document_version_id: 文档内容版本。
        figure_occurrence_id: 文档内图片 occurrence 节点。
        media_sha256: 图片内容摘要。
        source_node: 起点节点。
        target_node: 终点节点。
        relation_type: 稳定关系类型。
        direction: 候选方向。
        model_id: 原生解析器或视觉模型身份。
        policy_version: 生成候选的政策版本。

    Returns:
        `drel_` 前缀的稳定 128 位候选 ID。

    """
    payload = canonical_json(
        {
            "document_version_id": document_version_id,
            "figure_occurrence_id": figure_occurrence_id,
            "media_sha256": media_sha256,
            "source_node": source_node.model_dump(mode="json"),
            "target_node": target_node.model_dump(mode="json"),
            "relation_type": relation_type,
            "direction": direction.value,
            "model_id": model_id,
            "policy_version": policy_version,
        }
    )
    return "drel_" + hashlib.sha256(payload.encode()).hexdigest()[:32]


class ProductDiagramRelations:
    """在 Product 主库中隔离候选审阅与正式关系证据。"""

    def __init__(self, connections: SqliteConnectionFactory) -> None:
        self.connections = connections

    @property
    def policy_version(self) -> str:
        """返回正式证据发布政策版本。

        Args:
            无参数；读取当前固定发布政策。

        Returns:
            参与关系内容身份的版本字符串。

        """
        return _POLICY_VERSION

    def upsert_candidates(
        self, candidates: tuple[DiagramRelationCandidate, ...]
    ) -> tuple[DiagramRelationCandidate, ...]:
        """保存结构化候选，拒绝客户端伪造已接纳状态。

        Args:
            candidates: 原生连接器或视觉 Adapter 的待审候选。

        Returns:
            按输入顺序持久化的候选。

        Raises:
            PolicyDenied: 输入绕过人工/评测发布状态机。
            NotFound: 候选文档版本不属于声明的知识库和文档。

        """
        if any(
            item.review_state is not RelationReviewState.PENDING
            or item.reviewed_at is not None
            or item.reviewer_sha256 is not None
            for item in candidates
        ):
            raise PolicyDenied(
                "新图关系候选必须先进入待审状态。",
                stage="diagram_relation.candidate",
            )
        with self.connections.transaction(write=True) as connection:
            for item in candidates:
                row = connection.execute(
                    "SELECT d.knowledge_base_id FROM document_versions dv "
                    "JOIN documents d ON d.document_id=dv.document_id "
                    "WHERE dv.document_version_id=? AND d.document_id=? "
                    "AND d.knowledge_base_id=?",
                    (
                        item.document_version_id,
                        item.document_id,
                        item.knowledge_base_id,
                    ),
                ).fetchone()
                if row is None:
                    raise NotFound(
                        "图关系候选未绑定有效文档版本。",
                        stage="diagram_relation.scope",
                    )
                connection.execute(
                    "INSERT INTO diagram_relation_candidates("
                    "candidate_id, knowledge_base_id, document_id, "
                    "document_version_id, figure_occurrence_id, "
                    "media_sha256, review_state, payload_json, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(candidate_id) DO UPDATE SET "
                    "payload_json=CASE WHEN "
                    "diagram_relation_candidates.review_state='pending' "
                    "THEN excluded.payload_json ELSE payload_json END, "
                    "updated_at=CASE WHEN "
                    "diagram_relation_candidates.review_state='pending' "
                    "THEN excluded.updated_at ELSE updated_at END",
                    (
                        item.candidate_id,
                        item.knowledge_base_id,
                        item.document_id,
                        item.document_version_id,
                        item.figure_occurrence_id,
                        item.media_sha256,
                        item.review_state.value,
                        item.model_dump_json(),
                        item.created_at.isoformat(),
                        datetime.now(UTC).isoformat(),
                    ),
                )
        return candidates

    def list_candidates(
        self,
        knowledge_base_id: str,
        document_id: str,
        *,
        document_version_id: str | None = None,
    ) -> tuple[DiagramRelationCandidate, ...]:
        """按 KB、文档和可选版本读取有界候选清单。

        Args:
            knowledge_base_id: 候选所属知识库 ID。
            document_id: 候选所属文档 ID。
            document_version_id: 可选的精确文档版本 ID。

        Returns:
            按创建时间和候选 ID 排序的有界候选元组。

        """
        with self.connections.transaction() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM diagram_relation_candidates "
                "WHERE knowledge_base_id=? AND document_id=? "
                "AND (? IS NULL OR document_version_id=?) "
                "ORDER BY created_at, candidate_id LIMIT 1000",
                (
                    knowledge_base_id,
                    document_id,
                    document_version_id,
                    document_version_id,
                ),
            ).fetchall()
        return tuple(
            DiagramRelationCandidate.model_validate_json(str(row[0]))
            for row in rows
        )

    def review(
        self,
        candidate_id: str,
        state: RelationReviewState,
        *,
        reviewer_id: str,
    ) -> DiagramRelationCandidate:
        """显式接纳、拒绝或标记歧义，并保存主体摘要。

        Args:
            candidate_id: 待审候选 ID。
            state: 接纳、拒绝或歧义终态。
            reviewer_id: 当前审阅主体 ID；只保存摘要。

        Returns:
            已持久化的新候选状态；重复相同审阅返回当前状态。

        """
        if state is RelationReviewState.PENDING:
            raise PolicyDenied(
                "审阅不能把候选退回未审状态。",
                stage="diagram_relation.review",
            )
        with self.connections.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT payload_json FROM diagram_relation_candidates "
                "WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise NotFound(
                    "图关系候选不存在。", stage="diagram_relation.review"
                )
            current = DiagramRelationCandidate.model_validate_json(str(row[0]))
            if current.review_state is state:
                return current
            if state is RelationReviewState.ACCEPTED and (
                current.ambiguous
                or current.direction is RelationDirection.UNKNOWN
            ):
                raise PolicyDenied(
                    "方向未知或有歧义的图关系不能发布。",
                    stage="diagram_relation.review",
                )
            reviewed_at = datetime.now(UTC)
            reviewer_sha256 = hashlib.sha256(reviewer_id.encode()).hexdigest()
            updated = current.model_copy(
                update={
                    "ambiguous": (
                        True
                        if state is RelationReviewState.AMBIGUOUS
                        else current.ambiguous
                    ),
                    "review_state": state,
                    "review_revision": current.review_revision + 1,
                    "reviewed_at": reviewed_at,
                    "reviewer_sha256": reviewer_sha256,
                }
            )
            # model_copy 不执行校验，显式重新构造以守住发布门。
            updated = DiagramRelationCandidate.model_validate(
                updated.model_dump(mode="json")
            )
            connection.execute(
                "UPDATE diagram_relation_candidates SET review_state=?, "
                "payload_json=?, updated_at=? WHERE candidate_id=?",
                (
                    state.value,
                    updated.model_dump_json(),
                    reviewed_at.isoformat(),
                    candidate_id,
                ),
            )
        return updated

    def content_identity(self, knowledge_base_id: str) -> str | None:
        """只让正式接纳关系参与索引内容 Revision 身份。

        Args:
            knowledge_base_id: 待计算关系内容身份的知识库 ID。

        Returns:
            有接纳关系时返回 canonical SHA-256，否则返回 ``None``。

        """
        with self.connections.transaction() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM diagram_relation_candidates "
                "WHERE knowledge_base_id=? AND review_state='accepted' "
                "ORDER BY candidate_id",
                (knowledge_base_id,),
            ).fetchall()
        if not rows:
            return None
        candidates = tuple(
            DiagramRelationCandidate.model_validate_json(str(row[0]))
            for row in rows
        )
        return canonical_sha256(
            {
                "policy_version": _POLICY_VERSION,
                "accepted_relations": tuple(
                    _published_identity(item) for item in candidates
                ),
            }
        )

    def enrich_result(self, parsed: ParseResult) -> ParseResult:
        """只把匹配当前图像 occurrence 的已接纳关系加入可检索 IR。

        Args:
            parsed: Parser 返回的原始 Document IR 与报告。

        Returns:
            带已发布关系节点和同步报告的 ParseResult。

        """
        document = self.enrich(parsed.document_ir)
        return parsed.model_copy(
            update={"document_ir": document, "report": document.parse_report}
        )

    def enrich(self, document: DocumentIR) -> DocumentIR:
        """保存全部候选计数，但只发布无歧义且已接纳的关系。

        Args:
            document: 当前文档版本的不可变 Document IR。

        Returns:
            加入可证明关系节点和审阅汇总的新 Document IR。

        """
        candidates = self.list_candidates(
            document.document.knowledge_base_id,
            document.document.document_id,
            document_version_id=document.version.document_version_id,
        )
        states = Counter(item.review_state.value for item in candidates)
        nodes_by_id = {item.node_id: item for item in document.nodes}
        accepted: list[DiagramRelationCandidate] = []
        failures: Counter[str] = Counter()
        for candidate in candidates:
            if candidate.review_state is not RelationReviewState.ACCEPTED:
                continue
            image = nodes_by_id.get(candidate.figure_occurrence_id)
            if (
                image is None
                or image.kind is not NodeKind.IMAGE
                or image.image_attributes is None
                or image.image_attributes.content_sha256
                != candidate.media_sha256
            ):
                failures["DIAGRAM_RELATION_SOURCE_MISMATCH"] += 1
                continue
            accepted.append(candidate)
        nodes = list(document.nodes)
        positions = {item.node_id: index for index, item in enumerate(nodes)}
        added = 0
        for candidate in accepted:
            image_index = positions[candidate.figure_occurrence_id]
            image = nodes[image_index]
            relation_node = _relation_node(document, image, candidate)
            if relation_node.node_id in positions:
                continue
            nodes[image_index] = image.model_copy(
                update={"child_ids": (*image.child_ids, relation_node.node_id)}
            )
            positions[relation_node.node_id] = len(nodes)
            nodes.append(relation_node)
            added += 1
        metadata: dict[str, object] = dict(document.metadata)
        metadata["diagram_relation_enrichment"] = {
            "policy_version": _POLICY_VERSION,
            "candidate_count": len(candidates),
            "accepted_count": len(accepted),
            "published_count": added,
            "review_states": dict(sorted(states.items())),
            "source_mismatch_count": sum(failures.values()),
            "evidence_sources": sorted(
                {item.evidence_source.value for item in candidates}
            ),
        }
        report = document.parse_report
        issues = tuple(
            ParseIssue(
                code=code,
                severity="warning",
                action="exclude_relation_evidence",
                count=count,
                safe_message=(
                    "图关系与当前图片 occurrence 不匹配，未发布为证据。"
                ),
            )
            for code, count in sorted(failures.items())
        )
        updated_report = report.model_copy(
            update={
                "node_count": len(nodes),
                "visible_text_nodes": report.visible_text_nodes + added,
                "represented_visible_text_nodes": (
                    report.represented_visible_text_nodes + added
                ),
                "story_counts": _increment_body_count(
                    report.story_counts, added
                ),
                "issues": (*report.issues, *issues),
            }
        )
        return DocumentIR.model_validate(
            document.model_copy(
                update={
                    "nodes": tuple(nodes),
                    "parse_report": updated_report,
                    "metadata": freeze_json_object(metadata),
                }
            ).model_dump()
        )


def _relation_node(
    document: DocumentIR,
    image: DocumentNode,
    candidate: DiagramRelationCandidate,
) -> DocumentNode:
    text = _relation_text(candidate)
    return DocumentNode(
        node_id=deterministic_id(
            "node",
            document.version.document_version_id,
            image.node_id,
            candidate.candidate_id,
            _POLICY_VERSION,
        ),
        kind=NodeKind.PARAGRAPH,
        parent_node_id=image.node_id,
        order=len(image.child_ids),
        anchor=image.anchor.model_copy(
            update={
                "structural_path": (
                    *image.anchor.structural_path,
                    f"diagram-relation:{candidate.candidate_id}",
                ),
                "source_start_char": 0,
                "source_end_char": len(text),
            }
        ),
        text_payload=text_payload(text),
        metadata=freeze_json_object(
            {
                "origin": "diagram_relation",
                "source_kind": "diagram_relation",
                "candidate_id": candidate.candidate_id,
                "figure_occurrence_id": candidate.figure_occurrence_id,
                "media_sha256": candidate.media_sha256,
                "relation_type": candidate.relation_type,
                "direction": candidate.direction.value,
                "evidence_source": candidate.evidence_source.value,
                "model_id": candidate.model_id,
                "candidate_policy_version": candidate.policy_version,
                "publication_policy_version": _POLICY_VERSION,
                "review_state": candidate.review_state.value,
                "review_revision": candidate.review_revision,
            }
        ),
    )


def _published_identity(
    candidate: DiagramRelationCandidate,
) -> dict[str, object]:
    """排除 reviewer/timestamp 等不改变可检索语义的审计字段。"""
    return {
        "candidate_id": candidate.candidate_id,
        "document_version_id": candidate.document_version_id,
        "figure_occurrence_id": candidate.figure_occurrence_id,
        "media_sha256": candidate.media_sha256,
        "source_node": candidate.source_node.model_dump(mode="json"),
        "target_node": candidate.target_node.model_dump(mode="json"),
        "relation_type": candidate.relation_type,
        "direction": candidate.direction.value,
        "evidence_source": candidate.evidence_source.value,
        "model_id": candidate.model_id,
        "candidate_policy_version": candidate.policy_version,
        "publication_policy_version": _POLICY_VERSION,
    }


def _relation_text(candidate: DiagramRelationCandidate) -> str:
    source = candidate.source_node.label
    target = candidate.target_node.label
    relation = candidate.relation_type
    if candidate.direction is RelationDirection.SOURCE_TO_TARGET:
        return f"{source} --{relation}--> {target}"
    if candidate.direction is RelationDirection.TARGET_TO_SOURCE:
        return f"{target} --{relation}--> {source}"
    if candidate.direction is RelationDirection.BIDIRECTIONAL:
        return f"{source} <--{relation}--> {target}"
    raise ValueError("方向未知的候选不能成为正式关系证据。")


def _increment_body_count(
    story_counts: tuple[tuple[str, int], ...], added: int
) -> tuple[tuple[str, int], ...]:
    counts = Counter(dict(story_counts))
    counts["body"] += added
    return tuple(sorted(counts.items()))


__all__ = [
    "DiagramRelationCandidate",
    "DiagramRelationNode",
    "ProductDiagramRelations",
    "RelationDirection",
    "RelationEvidenceSource",
    "RelationReviewState",
    "relation_candidate_id",
]
