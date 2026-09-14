import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { api, type Evidence } from "../api/client";
import { PdfEvidenceSource } from "./PdfEvidenceSource";

const evidence: Evidence = {
  evidence_id: "S1",
  chunk_id: `chunk_${"1".repeat(32)}`,
  citation_text: "处理期限为 45 days。",
  source_label: "page12_only.pdf · PDF第12页 · PDF解析文字",
  source_spans: [],
  document_id: `doc_${"2".repeat(32)}`,
  document_version_id: `dver_${"3".repeat(32)}`,
  display_name: "page12_only.pdf",
  heading_path: [],
  table_context: false,
  page_index: 11,
  source_kind: "pdf_parsed_text",
  pdf_block_id: "page-12-answer",
  ocr_verification_state: null,
  selection_reason: "retrieval_candidate",
  publishable: true,
  retrieval_origins: ["exact"],
  quality_flags: [],
  metadata: [],
};

it("PDF 引用只打开证据绑定的文档版本和物理页", async () => {
  const list = vi.spyOn(api, "listArtifacts").mockResolvedValue({
    items: [
      {
        artifact_id: `sha256:${"4".repeat(64)}`,
        document_version_id: evidence.document_version_id!,
        media_type: "application/pdf",
        size_bytes: 2048,
        role: "source_document",
      },
    ],
  });

  render(
    <PdfEvidenceSource
      evidence={evidence}
      projectId="prj_project"
      kbId="kb_knowledge"
      token="session"
    />,
  );

  const link = await screen.findByRole("link", {
    name: "打开当前文档版本并定位到第 12 页",
  });
  expect(list).toHaveBeenCalledWith(
    "session",
    "prj_project",
    "kb_knowledge",
    evidence.document_id,
    evidence.document_version_id,
  );
  expect(link).toHaveAttribute(
    "href",
    expect.stringContaining(
      `document_version_id=${evidence.document_version_id}`,
    ),
  );
  expect(link).toHaveAttribute("href", expect.stringMatching(/#page=12$/));
});
