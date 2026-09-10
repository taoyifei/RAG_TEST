"""按媒体批准范围增补 OCR 节点，保留原生全文和可追溯图片关系。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from rag_app.adapters.providers.budget_ledger import ProviderBudgetLedger
from rag_app.adapters.providers.budget_models import BudgetCampaign
from rag_app.adapters.providers.budget_transport import (
    provider_budget_scope,
    provider_data_scope,
)
from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.errors import NotFound, RagError
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    DocumentIR,
    DocumentNode,
    NodeKind,
    ParsedArtifact,
    ParseResult,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.document import ParseIssue, text_payload
from rag_app.core.ports import BlobStorePort
from rag_app.product.model_settings import (
    KnowledgeBaseModelSettings,
    ProductModelSettings,
)
from rag_app.product.ocr_contract import (
    OcrAdapterIdentity,
    ProductOcrAdapter,
    ProductOcrPolicy,
    ProductOcrRecognition,
)
from rag_app.product.provider_runtime import ProviderRuntimeRegistry

_POLICY = "embedded-image-ocr-v1"


class ProductOcrEnrichment:
    """只使用受控解析制品和同一产品账本，单图失败不丢弃原文。"""

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        models: ProductModelSettings,
        providers: ProviderRuntimeRegistry,
        ledger_path: Path,
        artifacts: BlobStorePort | None = None,
    ) -> None:
        self.connections = connections
        self.models = models
        self.providers = providers
        self.artifacts = artifacts
        self.ledger_path = ledger_path

    def bind_blob_store(self, artifacts: BlobStorePort) -> None:
        """组合完成后绑定只读媒体入口，不接受调用方文件路径。

        Args:
            artifacts: 已由运行时创建的受控 Blob Store。

        Returns:
            无返回值；后续扫描使用该 Store 回读媒体。

        """
        self.artifacts = artifacts

    def content_identity(self, knowledge_base_id: str) -> str | None:
        """冻结启用模型、策略和批准/选择集合以创建独立的内容 Revision。

        Args:
            knowledge_base_id: 当前内容构建所属知识库。

        Returns:
            OCR 内容配置摘要；未启用 OCR 时返回 None。

        """
        settings = self.models.get(knowledge_base_id)
        if not settings.ocr_enabled:
            return None
        campaign = self._campaign(settings)
        policy = _policy(settings, egress_allowed=False)
        try:
            identity: dict[str, object] = self.providers.ocr_identity(
                settings.ocr_connection_id or "",
                model=policy.model,
                policy_version=policy.policy_version,
            ).model_dump(mode="json")
        except (ValueError, RagError) as error:
            # 不把 unavailable 降格为另一种 Provider；身份仍会稳定变化。
            identity = {
                "adapter": "unavailable",
                "provider": settings.ocr_connection_id or "unconfigured",
                "revision": _safe_error(error),
                "model": policy.model,
                "policy_version": policy.policy_version,
            }
        return canonical_sha256(
            {
                "adapter_identity": identity,
                "approved_media": ()
                if campaign is None
                else tuple(sorted(campaign.approved_media_hashes)),
                "selected_media": tuple(sorted(settings.ocr_media_hashes)),
                "ocr_revision": settings.ocr_revision,
            }
        )

    def enrich_result(self, parsed: ParseResult) -> ParseResult:
        """为 RevisionBuilder 保持 IR、报告和原始制品的同一事务结果。

        Args:
            parsed: 解析器生成的 IR、报告和受控媒体制品。

        Returns:
            保留原始制品且同步了增补 IR 与报告的解析结果。

        """
        document = self.enrich(parsed.document_ir, artifacts=parsed.artifacts)
        return parsed.model_copy(
            update={"document_ir": document, "report": document.parse_report}
        )

    def scan(
        self, knowledge_base_id: str, document_id: str | None = None
    ) -> dict[str, object]:
        """盘点活动 Revision 的去重媒体，不发送请求或返回图片正文。

        Args:
            knowledge_base_id: 只读扫描所属的知识库。
            document_id: 可选的活动文档筛选；省略时扫描整个知识库。

        Returns:
            去重媒体的安全属性、识别状态和待处理数量。

        """
        self.models.get(knowledge_base_id)
        with self.connections.transaction() as connection:
            rows = connection.execute(
                "SELECT rd.document_ir_json FROM revision_documents rd "
                "JOIN knowledge_bases kb "
                "ON kb.active_revision_id=rd.revision_id "
                "JOIN documents d ON d.document_id=rd.document_id "
                "WHERE kb.knowledge_base_id=? AND kb.deleted_at IS NULL "
                "AND d.deleted_at IS NULL AND d.status='active' "
                "AND (? IS NULL OR d.document_id=?)",
                (knowledge_base_id, document_id, document_id),
            ).fetchall()
        if document_id is not None and not rows:
            raise NotFound("活动文档不存在。", stage="ocr.scan")
        media: dict[str, dict[str, object]] = {}
        for row in rows:
            document = DocumentIR.model_validate_json(str(row[0]))
            entries = cast(
                tuple[dict[str, object], ...],
                self.scan_document(document)["media"],
            )
            for item in entries:
                media.setdefault(str(item["media_sha256"]), item)
        return _scan_summary(tuple(media.values()))

    def scan_document(
        self,
        document: DocumentIR,
        *,
        artifacts: tuple[ParsedArtifact, ...] = (),
    ) -> dict[str, object]:
        """在解析事务内检查媒体 SHA、格式、像素和缓存，不调用 Provider。

        Args:
            document: 保留媒体节点与来源身份的规范文档 IR。
            artifacts: 当前解析事务内尚未持久化的受控媒体制品。

        Returns:
            去重媒体的安全属性、批准状态、缓存状态及数量汇总。

        """
        settings = self.models.get(document.document.knowledge_base_id)
        policy = _policy(settings, egress_allowed=False)
        adapter, adapter_error = self._adapter(settings, policy)
        identity = None if adapter is None else adapter.identity
        campaign = self._campaign(settings)
        supplied = {item.artifact_id: item.content for item in artifacts}
        indexed_hashes = {
            str(metadata["media_sha256"])
            for node in document.nodes
            if (metadata := dict(node.metadata)).get("origin") == "ocr"
            and identity is not None
            and _metadata_identity(metadata) == identity
            and node.text
        }
        media: dict[str, dict[str, object]] = {}
        try:
            for node in document.nodes:
                if node.image_attributes is None:
                    continue
                attributes = node.image_attributes
                if attributes.content_sha256 in media:
                    continue
                content = self._content(attributes.blob_ref, supplied)
                entry: dict[str, object] = {
                    "media_sha256": attributes.content_sha256,
                    "artifact_id": attributes.blob_ref,
                    "part_uri": dict(node.metadata).get(
                        "media_part_uri", node.anchor.part_uri
                    ),
                    "media_type": attributes.media_type,
                    "size_bytes": len(content),
                    "width": None,
                    "height": None,
                    "supported": False,
                    "adapter_available": adapter is not None,
                    "adapter": None if identity is None else identity.adapter,
                    "provider": None if identity is None else identity.provider,
                    "ocr_revision": (
                        None if identity is None else identity.revision
                    ),
                    "approved": _approved(
                        campaign,
                        document,
                        attributes.content_sha256,
                        policy.model,
                    ),
                    "cached": (
                        identity is not None
                        and self._cached(
                            document, attributes.content_sha256, identity
                        )
                        is not None
                    ),
                    "indexed": attributes.content_sha256 in indexed_hashes,
                    "reason_code": adapter_error or "OCR_MEDIA_UNAVAILABLE",
                }
                if adapter is not None:
                    try:
                        inspected = adapter.inspect(
                            content,
                            media_type=attributes.media_type,
                            media_sha256=attributes.content_sha256,
                        )
                        entry.update(
                            width=inspected.width,
                            height=inspected.height,
                            supported=True,
                            reason_code=None,
                        )
                    except (ValueError, RagError) as error:
                        entry["reason_code"] = _safe_error(error)
                media[attributes.content_sha256] = entry
            return _scan_summary(tuple(media.values()))
        finally:
            if adapter is not None:
                adapter.close()

    def enrich(
        self,
        document: DocumentIR,
        *,
        artifacts: tuple[ParsedArtifact, ...] = (),
    ) -> DocumentIR:
        """对已批准的唯一图片至多执行一次识别，成功结果按知识库缓存。

        Args:
            document: 需要增补图片文字的规范文档 IR。
            artifacts: 当前解析事务内可按 artifact ID 回读的媒体制品。

        Returns:
            保留原生全文、媒体关系和逐图处理状态的增补文档 IR。

        """
        settings = self.models.get(document.document.knowledge_base_id)
        policy = _policy(settings, egress_allowed=True)
        adapter, adapter_error = self._adapter(settings, policy)
        campaign = self._campaign(settings)
        supplied = {item.artifact_id: item.content for item in artifacts}
        results: dict[str, ProductOcrRecognition | None] = {}
        failures: Counter[str] = Counter()
        nodes: list[DocumentNode] = []
        try:
            for node in document.nodes:
                attributes = node.image_attributes
                if attributes is None:
                    nodes.append(node)
                    continue
                sha = attributes.content_sha256
                if sha not in results:
                    result = (
                        self._cached(document, sha, adapter.identity)
                        if (
                            adapter is not None
                            and settings.ocr_enabled
                            and sha in settings.ocr_media_hashes
                        )
                        else None
                    )
                    if result is None:
                        if adapter is None:
                            result, reason = None, adapter_error
                        else:
                            result, reason = self._recognize(
                                document,
                                node,
                                settings,
                                campaign,
                                adapter,
                                supplied,
                            )
                        if reason:
                            failures[reason] += 1
                    results[sha] = result
                result = results[sha]
                if result is None:
                    nodes.append(node)
                    continue
                ocr_node = _ocr_node(document, node, result)
                # 不重复增补已经存在的同策略 OCR 子节点。
                if ocr_node.node_id in node.child_ids:
                    nodes.append(node)
                    continue
                nodes.append(
                    node.model_copy(
                        update={
                            "child_ids": (*node.child_ids, ocr_node.node_id)
                        }
                    )
                )
                nodes.append(ocr_node)
            return _enriched_ir(document, tuple(nodes), results, failures)
        finally:
            if adapter is not None:
                adapter.close()

    def _content(self, artifact_id: str, supplied: dict[str, bytes]) -> bytes:
        if artifact_id in supplied:
            return supplied[artifact_id]
        stored = (
            None if self.artifacts is None else self.artifacts.read(artifact_id)
        )
        return b"" if stored is None else stored.content

    def _campaign(
        self, settings: KnowledgeBaseModelSettings
    ) -> BudgetCampaign | None:
        if not settings.budget_campaign_id or not self.ledger_path.exists():
            return None
        try:
            return ProviderBudgetLedger(
                self.ledger_path, read_only=True
            ).campaign(settings.budget_campaign_id)
        except (ValueError, RagError):
            return None

    def _adapter(
        self,
        settings: KnowledgeBaseModelSettings,
        policy: ProductOcrPolicy,
    ) -> tuple[ProductOcrAdapter | None, str | None]:
        """解析一次 OCR adapter；配置错误转成逐图安全原因码。"""
        if not settings.ocr_connection_id or not settings.ocr_model:
            return None, "OCR_ADAPTER_UNAVAILABLE"
        try:
            return (
                self.providers.ocr_adapter(
                    settings.ocr_connection_id,
                    model=policy.model,
                    config=policy,
                ),
                None,
            )
        except (ValueError, RagError) as error:
            return None, _safe_error(error)

    def _cached(
        self,
        document: DocumentIR,
        sha: str,
        identity: OcrAdapterIdentity,
    ) -> ProductOcrRecognition | None:
        with self.connections.transaction() as connection:
            row = connection.execute(
                "SELECT recognition_json FROM ocr_enrichment_cache "
                "WHERE knowledge_base_id=? AND media_sha256=? "
                "AND adapter=? AND provider=? AND ocr_revision=? "
                "AND model=? AND policy_version=?",
                (
                    document.document.knowledge_base_id,
                    sha,
                    identity.adapter,
                    identity.provider,
                    identity.revision,
                    identity.model,
                    identity.policy_version,
                ),
            ).fetchone()
        if row is None:
            return None
        result = ProductOcrRecognition.model_validate_json(str(row[0]))
        if result.media_sha256 != sha or result.adapter_identity != identity:
            return None
        return result

    def _recognize(  # noqa: PLR0913, PLR0917
        self,
        document: DocumentIR,
        node: DocumentNode,
        settings: KnowledgeBaseModelSettings,
        campaign: BudgetCampaign | None,
        adapter: ProductOcrAdapter,
        supplied: dict[str, bytes],
    ) -> tuple[ProductOcrRecognition | None, str | None]:
        attributes = node.image_attributes
        if attributes is None:
            return None, "OCR_MEDIA_UNAVAILABLE"
        sha = attributes.content_sha256
        if not settings.ocr_enabled or sha not in settings.ocr_media_hashes:
            return (
                None,
                "OCR_MEDIA_NOT_SELECTED"
                if settings.ocr_enabled
                else "OCR_DISABLED",
            )
        if (
            not _approved(campaign, document, sha, adapter.identity.model)
            or campaign is None
        ):
            return None, "OCR_MEDIA_NOT_APPROVED"
        try:
            content = self._content(attributes.blob_ref, supplied)
            adapter.inspect(
                content,
                media_type=attributes.media_type,
                media_sha256=sha,
            )
            ledger = ProviderBudgetLedger(self.ledger_path)
            with (
                provider_budget_scope(
                    ledger,
                    campaign_id=campaign.campaign_id,
                    authorization_id=campaign.authorization_id,
                    scope=campaign.scope,
                    step_id="image.ocr",
                ),
                provider_data_scope(
                    project_id=document.document.project_id,
                    knowledge_base_id=document.document.knowledge_base_id,
                    source_hashes=(document.source.content_sha256,),
                    media_hashes=(sha,),
                ),
            ):
                result = adapter.recognize(
                    content, media_type=attributes.media_type, media_sha256=sha
                )
            if (
                not result.complete
                or result.media_sha256 != sha
                or result.adapter_identity != adapter.identity
            ):
                return None, "OCR_OUTPUT_INCOMPLETE"
            with self.connections.transaction(write=True) as connection:
                connection.execute(
                    "INSERT INTO ocr_enrichment_cache("
                    "knowledge_base_id, media_sha256, adapter, provider, "
                    "ocr_revision, model, policy_version, recognition_json, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT DO NOTHING",
                    (
                        document.document.knowledge_base_id,
                        sha,
                        adapter.identity.adapter,
                        adapter.identity.provider,
                        adapter.identity.revision,
                        adapter.identity.model,
                        adapter.identity.policy_version,
                        result.model_dump_json(),
                        datetime.now(UTC).isoformat(),
                    ),
                )
            return result, None
        except (ValueError, RagError, OSError) as error:
            return None, _safe_error(error)


def _approved(
    campaign: BudgetCampaign | None, document: DocumentIR, sha: str, model: str
) -> bool:
    if (
        campaign is None
        or campaign.scope_mode != "knowledge_base"
        or not campaign.expires_at
    ):
        return False
    return (
        campaign.project_id == document.document.project_id
        and campaign.knowledge_base_id == document.document.knowledge_base_id
        and document.source.content_sha256 in campaign.approved_source_hashes
        and sha in campaign.approved_media_hashes
        and model in campaign.allowed_models
        and "image.ocr" in campaign.allowed_operations
        and datetime.fromisoformat(campaign.expires_at) > datetime.now(UTC)
    )


def _ocr_node(
    document: DocumentIR,
    image: DocumentNode,
    result: ProductOcrRecognition,
) -> DocumentNode:
    attributes = image.image_attributes
    if attributes is None:
        raise ValueError("OCR_MEDIA_UNAVAILABLE")
    return DocumentNode(
        node_id=deterministic_id(
            "node",
            document.version.document_version_id,
            image.node_id,
            result.media_sha256,
            result.adapter,
            result.provider,
            result.revision,
            result.model,
            result.policy_version,
        ),
        kind=NodeKind.PARAGRAPH,
        parent_node_id=image.node_id,
        order=len(image.child_ids),
        anchor=image.anchor.model_copy(
            update={
                "structural_path": (*image.anchor.structural_path, "ocr:0"),
                "source_start_char": 0,
                "source_end_char": len(result.text),
            }
        ),
        text_payload=text_payload(result.text),
        metadata=freeze_json_object(
            _ocr_metadata(image, result, artifact_id=attributes.blob_ref)
        ),
    )


def _ocr_metadata(
    image: DocumentNode,
    result: ProductOcrRecognition,
    *,
    artifact_id: str,
) -> dict[str, object]:
    """只把 Provider 实际返回的可选版面字段写入派生节点。"""
    metadata: dict[str, object] = {
        "origin": "ocr",
        "source_kind": "ocr_text",
        "media_sha256": result.media_sha256,
        "artifact_id": artifact_id,
        "media_part_uri": dict(image.metadata).get(
            "media_part_uri", image.anchor.part_uri
        ),
        "adapter": result.adapter,
        "provider": result.provider,
        "ocr_revision": result.revision,
        "model": result.model,
        "policy_version": result.policy_version,
    }
    if result.confidence is not None:
        metadata["confidence"] = result.confidence
    if result.bbox is not None:
        metadata["bbox"] = list(result.bbox)
    if result.lines:
        metadata["lines"] = [
            {
                "text": line.text,
                **(
                    {}
                    if line.confidence is None
                    else {"confidence": line.confidence}
                ),
                **({} if line.bbox is None else {"bbox": list(line.bbox)}),
            }
            for line in result.lines
        ]
    return metadata


def _enriched_ir(
    document: DocumentIR,
    nodes: tuple[DocumentNode, ...],
    results: dict[str, ProductOcrRecognition | None],
    failures: Counter[str],
) -> DocumentIR:
    if not results:
        return document
    added = len(nodes) - len(document.nodes)
    report = document.parse_report
    issues = tuple(
        ParseIssue(
            code=code,
            severity="warning",
            action="retain_native_text",
            count=count,
            safe_message="图片文字尚未完成识别，原生文字已保留。",
        )
        for code, count in sorted(failures.items())
    )
    stories = Counter(dict(report.story_counts))
    original_ids = {item.node_id for item in document.nodes}
    for node in nodes:
        if node.node_id not in original_ids:
            stories[node.anchor.story_kind.value] += 1
    updated_report = report.model_copy(
        update={
            "node_count": len(nodes),
            "visible_text_nodes": report.visible_text_nodes + added,
            "represented_visible_text_nodes": (
                report.represented_visible_text_nodes + added
            ),
            "story_counts": tuple(sorted(stories.items())),
            "issues": (*report.issues, *issues),
        }
    )
    metadata = dict(document.metadata)
    metadata["ocr_enrichment"] = {
        "status": "PARTIAL"
        if any(result is None for result in results.values())
        else "COMPLETE",
        "media_count": len(results),
        "recognized_count": sum(
            result is not None for result in results.values()
        ),
        "pending_count": sum(result is None for result in results.values()),
        "policy_version": _POLICY,
    }
    return DocumentIR.model_validate(
        document.model_copy(
            update={
                "nodes": nodes,
                "parse_report": updated_report,
                "metadata": freeze_json_object(metadata),
            }
        ).model_dump()
    )


def _scan_summary(media: tuple[dict[str, object], ...]) -> dict[str, object]:
    return {
        "media": media,
        "media_count": len(media),
        "recognized_count": sum(bool(item["cached"]) for item in media),
        "pending_count": sum(not item["cached"] for item in media),
        "indexed_count": sum(bool(item["indexed"]) for item in media),
        "rebuild_count": sum(
            bool(item["cached"]) and not item["indexed"] for item in media
        ),
    }


def _safe_error(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.replace("_", "").isalnum():
        return code
    message = str(error)
    return (
        message
        if message.startswith("OCR_") and message.replace("_", "").isalnum()
        else "OCR_RECOGNITION_FAILED"
    )


def _policy(
    settings: KnowledgeBaseModelSettings, *, egress_allowed: bool
) -> ProductOcrPolicy:
    return ProductOcrPolicy(
        model=settings.ocr_model or "qwen3.5-ocr",
        policy_version=_POLICY,
        egress_allowed=egress_allowed,
    )


def _metadata_identity(
    metadata: Mapping[str, object],
) -> OcrAdapterIdentity | None:
    try:
        return OcrAdapterIdentity(
            adapter=str(metadata["adapter"]),
            provider=str(metadata["provider"]),
            revision=str(metadata["ocr_revision"]),
            model=str(metadata["model"]),
            policy_version=str(metadata["policy_version"]),
        )
    except (KeyError, ValueError):
        return None
