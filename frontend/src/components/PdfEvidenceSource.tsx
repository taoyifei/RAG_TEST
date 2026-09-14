import { useEffect, useState } from "react";

import { api, type ArtifactDescriptor, type Evidence } from "../api/client";
import { ErrorPanel } from "./ui";

export function isPdfEvidence(evidence: Evidence): boolean {
  return (
    ["pdf_parsed_text", "ocr_text"].includes(evidence.source_kind ?? "") &&
    evidence.page_index !== null &&
    evidence.page_index !== undefined
  );
}

export function PdfEvidenceSource({
  evidence,
  projectId,
  kbId,
  token,
}: {
  evidence: Evidence;
  projectId: string;
  kbId: string;
  token: string;
}) {
  const [artifact, setArtifact] = useState<ArtifactDescriptor>();
  const [error, setError] = useState<unknown>();
  useEffect(() => {
    const documentId = evidence.document_id;
    const versionId = evidence.document_version_id;
    if (!isPdfEvidence(evidence) || !documentId || !versionId) return;
    let active = true;
    void api
      .listArtifacts(token, projectId, kbId, documentId, versionId)
      .then((result) => {
        if (!active) return;
        const source = result.items.find(
          (item) =>
            item.role === "source_document" &&
            item.media_type === "application/pdf",
        );
        if (!source) throw new Error("当前 PDF 版本的原始文件不可用。");
        setArtifact(source);
      })
      .catch((reason) => {
        if (active) setError(reason);
      });
    return () => {
      active = false;
    };
  }, [evidence, kbId, projectId, token]);
  if (!isPdfEvidence(evidence)) return null;
  const documentId = evidence.document_id;
  const versionId = evidence.document_version_id;
  const pageNumber = (evidence.page_index ?? 0) + 1;
  return (
    <section className="panel">
      <h3>PDF 原页</h3>
      <p>
        物理第 {pageNumber} 页 ·{" "}
        {evidence.source_kind === "ocr_text" ? "OCR识别文字" : "PDF解析文字"}
      </p>
      {artifact && documentId && versionId ? (
        <a
          className="button-link"
          href={api.documentArtifactUrl(
            projectId,
            kbId,
            documentId,
            versionId,
            artifact.artifact_id,
            pageNumber,
          )}
          target="_blank"
          rel="noreferrer"
        >
          打开当前文档版本并定位到第 {pageNumber} 页
        </a>
      ) : error !== undefined ? (
        <ErrorPanel error={error} />
      ) : (
        <p role="status">正在核对当前文档版本的原始 PDF…</p>
      )}
    </section>
  );
}
