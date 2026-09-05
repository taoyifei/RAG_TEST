"""独立 P11 pilot 的预标注公开语料；复用 Evaluation V3 模型。"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from pydantic import BaseModel, ConfigDict

from evaluation.v2.models import (
    DatasetDocument,
    DatasetManifest,
    EvaluationCase,
)
from evaluation.v2.runtime import _effective_cases
from rag_app.adapters.chunkers.docx_structural import DocxStructuralChunker
from rag_app.adapters.parsers.word_document import WordDocumentV1Parser
from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.reranking import _bounded_text
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    ChunkingContext,
    ChunkingPolicy,
    DocumentRef,
    HydratedChunk,
    KnowledgeBaseScope,
    ParseContext,
    ParseSource,
    RankedChunk,
    RetrievalPolicy,
    SearchRequest,
)
from rag_app.core.models.chunk import Chunk
from rag_app.core.policies import ParsingPolicy

_DATASET = Path(__file__).parent / "datasets" / "p11-pilot"
_REQUIRED_CATEGORIES = {
    "identifier_free_fact",
    "semantic_paraphrase",
    "cjk_noise",
    "table_structure",
    "negative_refusal",
    "revision_isolation",
    "scope_isolation",
}
_MIN_POSITIVE = 20
_MIN_NEGATIVE = 10


class PilotContent(BaseModel):
    """只允许内联公开段落和表格，不接受外部文件路径。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    paragraphs: tuple[str, ...]
    tables: tuple[tuple[tuple[str, ...], ...], ...]

    def docx_bytes(self) -> bytes:
        """生成无外链且字节稳定的公开合成 DOCX。

        Args:
            无参数；使用已加载的公开内容。

        Returns:
            可供现有上传或索引入口使用的 DOCX 字节。

        """

        def paragraph(text: str) -> str:
            """为公开文本生成转义后的 OOXML 段落。

            Args:
                text: 预先固定的合成正文。

            Returns:
                可嵌入 document.xml 的段落元素。

            """
            return f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>"

        body = "".join(paragraph(text) for text in self.paragraphs)
        for rows in self.tables:
            columns = max(len(row) for row in rows)
            body += "<w:tbl><w:tblGrid>" + "<w:gridCol/>" * columns
            body += "</w:tblGrid>"
            for row in rows:
                body += (
                    "<w:tr>"
                    + "".join(f"<w:tc>{paragraph(cell)}</w:tc>" for cell in row)
                    + "</w:tr>"
                )
            body += "</w:tbl>"
        document = (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>'
            + body
            + "<w:sectPr/></w:body></w:document>"
        )
        content_types = (
            '<Types xmlns="http://schemas.openxmlformats.org/package/'
            '2006/content-types"><Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>'
        )
        relationships = (
            '<Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="rIdDocument" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>"
        )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, text in (
                ("[Content_Types].xml", content_types),
                ("_rels/.rels", relationships),
                ("word/document.xml", document),
            ):
                archive.writestr(zipfile.ZipInfo(name), text.encode("utf-8"))
        return output.getvalue()


@dataclass(frozen=True, slots=True)
class PilotDocument:
    """一个固定逻辑文档和公开版本内容。"""

    document: DatasetDocument
    versions: tuple[PilotContent, ...]

    def content(self, version: int = -1) -> bytes:
        """返回指定公开版本的 DOCX 字节，默认当前版本。

        Args:
            version: 公开版本的序号；负一表示当前版本。

        Returns:
            确定性合成 DOCX 字节。

        """
        return self.versions[version].docx_bytes()


@dataclass(frozen=True, slots=True)
class PilotDataset:
    """固定 holdout 标签、语料与包含两者的摘要。"""

    manifest: DatasetManifest
    cases: tuple[EvaluationCase, ...]
    documents: tuple[PilotDocument, ...]
    dataset_sha256: str


def load_pilot_dataset(path: Path = _DATASET) -> PilotDataset:
    """校验独立 pilot，保留现有大数据集的完整覆盖门。

    Args:
        path: 只包含公开 JSON 语料和预先固定标签的目录。

    Returns:
        不进行检索、不调用 Provider 的独立数据集。

    Raises:
        ValueError: 标签、身份、样本数或场景覆盖不完整。

    """
    manifest = DatasetManifest.model_validate_json(
        (path / "manifest.json").read_text("utf-8")
    )
    cases = tuple(
        EvaluationCase.model_validate_json(line)
        for line in (path / "cases.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    )
    raw_contents = json.loads((path / "corpus.json").read_text("utf-8"))
    contents = {
        key: PilotContent.model_validate(value)
        for key, value in raw_contents.items()
    }
    documents = tuple(
        PilotDocument(
            document,
            tuple(
                contents[version.fixture_id] for version in document.versions
            ),
        )
        for document in manifest.documents
    )
    _validate_labels(manifest, cases, documents)
    return PilotDataset(
        manifest,
        cases,
        documents,
        canonical_sha256(
            {
                "manifest": manifest.model_dump(mode="json"),
                "cases": [case.model_dump(mode="json") for case in cases],
                "corpus": raw_contents,
            }
        ),
    )


def bind_pilot_cases(
    dataset: PilotDataset, chunks: dict[str, Chunk]
) -> tuple[EvaluationCase, ...]:
    """在查询之前由 active inventory 绑定独立来源标签。

    Args:
        dataset: 已校验的 pilot，标签来自静态 JSON。
        chunks: 当前索引的 canonical Chunk inventory，不是查询 Evidence。

    Returns:
        带实际 Chunk ID 和精确 SourceSpan 的 V3 Case。

    """
    return _effective_cases(dataset.cases, chunks, require_fixed_labels=False)


def approved_pilot_texts(
    retrieval_policy: RetrievalPolicy | None = None,
) -> tuple[str, ...]:
    """在本地纯解析公开语料，生成当前实际 Provider 文本批准集。

    Args:
        retrieval_policy: 当前方案的重排截断规则；默认产品规则。

    Returns:
        问题、全部公开版本的实际 embedding 文本与重排候选文本；
        不构建 Runtime，不读 Credential，也不访问 HTTP。

    """
    dataset = load_pilot_dataset()
    texts = {case.query for case in dataset.cases}
    analyzer = QueryAnalyzer()
    for case in dataset.cases:
        analysis = analyzer.analyze(
            SearchRequest(
                scope=KnowledgeBaseScope(
                    project_id=case.project_id,
                    knowledge_base_id=case.knowledge_base_id,
                ),
                text=case.query,
            )
        )
        texts.add(analysis.normalized_query)
    for item in dataset.documents:
        for index, version in enumerate(item.document.versions):
            texts.update(
                approved_docx_texts(
                    item.content(index), version.display_name, retrieval_policy
                )
            )
    return tuple(sorted(texts))


def approved_docx_texts(
    content: bytes,
    display_name: str,
    retrieval_policy: RetrievalPolicy | None = None,
) -> tuple[str, ...]:
    """以产品 Parser/Chunker 解析调用方传入的公开合成 DOCX。

    Args:
        content: Runner 已固定并获准发送的公开合成字节。
        display_name: 实际上传显示名，影响 embedding 上下文。
        retrieval_policy: 当前方案的重排文本截断规则。

    Returns:
        去重后的 embedding 与 rerank 实际文本视图，完全本地计算。

    """
    policy = retrieval_policy or RetrievalPolicy()
    parser = WordDocumentV1Parser()
    chunker = DocxStructuralChunker(ChunkingPolicy())
    reference = DocumentRef(
        project_id=deterministic_id("prj", "p11-public-payload-approval"),
        knowledge_base_id=deterministic_id("kb", "p11-public-payload-approval"),
        document_id=deterministic_id(
            "doc", "p11-public-payload-approval", display_name
        ),
        display_name=display_name,
    )
    parsed = parser.parse(
        ParseSource(
            media_type="application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
            display_name=display_name,
            extension=".docx",
            content=content,
        ),
        ParsingPolicy(),
        ParseContext(document=reference),
    )
    result = chunker.chunk(
        parsed.document_ir,
        ChunkingContext(
            chunker_fingerprint=chunker.fingerprint,
            index_revision_id=deterministic_id(
                "irev", "p11-public-payload-approval", display_name
            ),
        ),
    )
    texts = set()
    for chunk in result.chunks:
        texts.add(chunk.embedding_text)
        texts.add(
            _bounded_text(
                RankedChunk(
                    hydrated=HydratedChunk(
                        chunk=chunk, display_name=display_name
                    ),
                    fusion_rank=1,
                ),
                policy.rerank_text_char_limit,
            )
        )
    return tuple(sorted(texts))


def _validate_labels(
    manifest: DatasetManifest,
    cases: tuple[EvaluationCase, ...],
    documents: tuple[PilotDocument, ...],
) -> None:
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("Pilot case_id 重复。")
    if len({case.query for case in cases}) != len(cases):
        raise ValueError("Pilot 必须按不同问题计样本，不能复制增大样本数。")
    if any(case.split != "holdout" for case in cases):
        raise ValueError("独立 pilot 禁止混入 tuning 标签。")
    positive = sum(case.expected.answerable for case in cases)
    if positive < _MIN_POSITIVE or len(cases) - positive < _MIN_NEGATIVE:
        raise ValueError("Pilot 不得降低已接受的正例与负例样本门。")
    if not {case.category for case in cases} >= _REQUIRED_CATEGORIES:
        raise ValueError("Pilot 缺少本阶段必需场景。")
    known = {item.document_id: item for item in manifest.documents}
    active_texts = {
        item.document.document_id: json.dumps(
            item.versions[-1].model_dump(mode="json"), ensure_ascii=False
        )
        for item in documents
    }
    scopes = {
        (item.project_id, item.knowledge_base_id) for item in known.values()
    }
    for case in cases:
        if (case.project_id, case.knowledge_base_id) not in scopes:
            raise ValueError("Pilot Case scope 不属于语料。")
        references = {
            *case.expected.relevant_document_ids,
            *case.constraints.forbidden_document_ids,
        }
        if not references <= known.keys():
            raise ValueError("Pilot 标签引用了未知文档。")
        for item in case.expected.required_source_ranges:
            if item.exact_text not in active_texts[item.document_id]:
                raise ValueError("Pilot 来源标签不在当前版本。")
            if known[item.document_id].family_group_id != case.group_id:
                raise ValueError("Pilot 文档族与标签不一致。")
