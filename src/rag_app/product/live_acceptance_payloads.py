"""P11 固定公开数据与 Provider 请求批准集；不访问用户文档。"""

from __future__ import annotations

from io import BytesIO

from docx import Document

from evaluation.p11_pilot_data import approved_docx_texts, approved_pilot_texts
from rag_app.adapters.providers.budget_transport import (
    payload_contract,
    provider_request_identity,
)
from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    KnowledgeBaseScope,
    RetrievalPolicy,
    SearchRequest,
)
from rag_app.product.models import ProviderConnection
from rag_app.product.provider_runtime import _base_url, _path, _payload
from rag_app.product.resolved_profile import ResolvedEmbeddingSpec

PUBLIC_DOCUMENT = "验收示例：设备借用申请经负责人审批后，由档案员归档。"
DOCUMENT_NAME = "P11-公开合成验收.docx"
QUERY_LIMIT = 5
QUERIES = {
    "primary_query": "设备借用申请如何归档？",
    "standby_failover": "设备借用经过审批以后由谁保存？",
    "recovery": "设备借用记录最终交给谁归档？",
    "standby_unavailable": "借用审批完成以后归档的负责人是谁？",
}


def functional_document() -> bytes:
    """生成仅包含仓库常量的公开合成 DOCX。

    Args:
        无参数；使用固定公开段落。

    Returns:
        DOCX 字节，不打开文件路径。

    """
    document = Document()
    document.add_paragraph(PUBLIC_DOCUMENT)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def approved_payload_contracts(
    connections: tuple[ProviderConnection, ...],
    specs: tuple[ResolvedEmbeddingSpec, ...],
    retrieval_policy: RetrievalPolicy | None = None,
    credential_versions: dict[str, int] | None = None,
) -> dict[str, tuple[str, ...]]:
    """从实际请求策略与纯本地解析生成完整公开文本和形状批准集。

    Args:
        connections: 当前页面连接的非 Secret 元数据。
        specs: 各连接对应的实际 Embedding 解析策略。
        retrieval_policy: 方案的实际重排文本截断规则。
        credential_versions: 当前页面凭据版本，只含版本号。

    Returns:
        精确 Probe Payload、公开文本和模型参数形状的 SHA256 集合。

    """
    texts = set(QUERIES.values())
    analyzer = QueryAnalyzer()
    for text in QUERIES.values():
        texts.add(
            analyzer.analyze(
                SearchRequest(
                    scope=KnowledgeBaseScope(
                        project_id=deterministic_id("prj", "p11-approval"),
                        knowledge_base_id=deterministic_id(
                            "kb", "p11-approval"
                        ),
                    ),
                    text=text,
                )
            ).normalized_query
        )
    texts.update(approved_pilot_texts(retrieval_policy))
    texts.update(
        approved_docx_texts(
            functional_document(), DOCUMENT_NAME, retrieval_policy
        )
    )
    payload_hashes: set[str] = set()
    text_hashes = {canonical_sha256(text) for text in texts}
    shape_hashes: set[str] = set()
    request_identities: set[str] = set()
    for connection, spec in zip(connections, specs, strict=True):
        payloads = [
            _payload(connection, operation, spec.model, resolved=spec)
            for operation in ("embedding.document", "embedding.query")
        ]
        if connection.provider_type == "jina":
            rerank = _payload(connection, "reranking", "jina-reranker-v3.5")
            payloads.extend(
                {**rerank, "top_n": top_n}
                for top_n in range(1, QUERY_LIMIT + 1)
            )
        for payload in payloads:
            payload_hash, approved_texts, shape = payload_contract(payload)
            payload_hashes.add(payload_hash)
            text_hashes.update(approved_texts)
            if shape is not None:
                shape_hashes.add(shape)
            operation = (
                "reranking" if "documents" in payload else "embedding.document"
            )
            if credential_versions is not None:
                request_identities.add(
                    provider_request_identity(
                        _base_url(connection)
                        + _path(connection.provider_type, operation),
                        payload["model"],
                        {
                            "connection_id": connection.connection_id,
                            "configuration_version": (
                                connection.configuration_version
                            ),
                            "credential_key_version": credential_versions[
                                connection.credential_id
                            ],
                        },
                    )
                )
    return {
        "approved_payload_hashes": tuple(sorted(payload_hashes)),
        "approved_text_hashes": tuple(sorted(text_hashes)),
        "approved_request_shape_hashes": tuple(sorted(shape_hashes)),
        "approved_request_identities": tuple(sorted(request_identities)),
    }
