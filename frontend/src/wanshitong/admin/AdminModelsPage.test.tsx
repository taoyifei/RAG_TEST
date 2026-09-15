import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AdminModelsPage } from "./AdminModelsPage";

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.restoreAllMocks());

describe("湾事通模型只读页", () => {
  it("展示脱敏配置且不提供写入或主动验证入口", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response({
        embedding: {
          configured: true,
          connection_name: "内部 Embedding",
          model: "embedding-v3",
          dimension: 1024,
          host: "embed…internal",
          validation_status: "live_validated",
        },
        reranker: {
          configured: true,
          connection_name: "内部 Reranker",
          model: "reranker-v2",
          protocol: "tei",
          path: "/rerank",
          host: "rank…internal",
          validation_status: "live_validated",
        },
        llm: {
          configured: true,
          connection_name: "内部 LLM",
          model: "qwen-internal",
          host: "llm…internal",
          validation_status: "live_validated",
        },
        retrieval_profile: {
          profile_revision_id: "rprof_fixed",
          status: "active",
        },
        kb_model_settings: {
          generation_model: "qwen-internal",
          rewrite_enabled: false,
        },
        image_ocr: { enabled: false, message: "当前 Demo 未启用" },
        pdf_parser: { enabled: false, message: "当前 Demo 未启用" },
      }),
    );
    render(<AdminModelsPage />);

    expect(await screen.findByText("embedding-v3")).toBeVisible();
    expect(screen.getByText("1024")).toBeVisible();
    expect(screen.getByText("tei / /rerank")).toBeVisible();
    expect(screen.getByText("llm…internal")).toBeVisible();
    expect(screen.getByText("rprof_fixed")).toBeVisible();
    for (const label of ["新增", "编辑", "删除", "激活", "验证", "保存"] as const) {
      expect(screen.queryByRole("button", { name: new RegExp(label) })).not.toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "刷新" })).toBeVisible();
  });
});
