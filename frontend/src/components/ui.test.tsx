import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Evidence } from "../api/client";
import { EvidenceDrawer, StatusBadge } from "./ui";

const evidence = {
  evidence_id: "ev-1",
  chunk_id: "chunk_00000000000000000000000000000000",
  citation_text: "青岛啤酒采购流程需要采购申请审批",
  source_label: "青岛啤酒采购流程.docx",
  source_spans: [],
  heading_path: [],
  table_context: false,
  selection_reason: "retrieval_candidate",
  publishable: true,
  retrieval_origins: ["lexical"],
  quality_flags: [],
  metadata: [],
} satisfies Evidence;

describe("控制台基础组件", () => {
  it("Evidence Drawer 展示引用、原因和发布状态并支持 Escape", () => {
    const close = vi.fn();
    render(<EvidenceDrawer evidence={evidence} onClose={close} />);
    expect(
      screen.getByRole("dialog", { name: "证据详情" }),
    ).toBeInTheDocument();
    expect(screen.getByText(evidence.citation_text)).toBeInTheDocument();
    expect(screen.getByText("retrieval_candidate")).toBeInTheDocument();
    expect(screen.getByText("可发布")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(close).toHaveBeenCalledOnce();
  });

  it("未验证状态不会渲染为成功", () => {
    render(<StatusBadge value="not_verified" />);
    expect(screen.getByText("尚未验证")).toHaveClass("neutral");
  });

  it("未知状态保持中性，已知健康状态显示中文", () => {
    render(
      <>
        <StatusBadge value="future_status" />
        <StatusBadge value="healthy" />
      </>,
    );
    expect(screen.getByText("未知状态（技术详情可查看原值）")).toHaveClass(
      "neutral",
    );
    expect(screen.getByText("运行正常")).toHaveClass("good");
  });
});
