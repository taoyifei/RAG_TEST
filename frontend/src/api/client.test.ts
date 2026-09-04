import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, readSseResponse } from "./client";

afterEach(() => vi.restoreAllMocks());

describe("API 客户端契约", () => {
  it("新文档与新版本使用不同端点", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ revision_id: "irev_0" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    const file = new File(["docx"], "流程.docx", { type: "application/docx" });
    await api.uploadDocument("admin", "prj", "kb", file, "same-key");
    await api.uploadVersion("admin", "prj", "kb", "doc", file, "same-key");

    expect(fetchMock.mock.calls[0]?.[0]).toContain("/documents?display_name=");
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "/api/v1/projects/prj/knowledge-bases/kb/documents/doc/versions",
    );
    for (const call of fetchMock.mock.calls) {
      expect(new Headers(call[1]?.headers).get("Idempotency-Key")).toBe(
        "same-key",
      );
    }
  });

  it("保留统一错误信封中的稳定字段", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "INDEX_NOT_READY",
            message: "索引尚未就绪",
            stage: "retrieval.snapshot",
            retryable: true,
            trace_id: "trace_safe",
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    const error = await api
      .search("query", "prj", "kb", "测试")
      .catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      code: "INDEX_NOT_READY",
      retryable: true,
      traceId: "trace_safe",
    });
  });

  it("把 AbortSignal 传给检索请求", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ evidence: [] }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    const controller = new AbortController();

    await api.search("query", "prj", "kb", "测试", controller.signal);

    expect(fetchMock.mock.calls[0]?.[1]?.signal).toBe(controller.signal);
  });

  it("只以 final 事件作为 SSE 最终结果", async () => {
    const payload = { trace_id: "trace_1", evidence: [], answer: "完成" };
    const response = new Response(
      `event: meta\ndata: {"trace_id":"trace_1"}\n\nevent: final\ndata: ${JSON.stringify(payload)}\n\n`,
    );
    await expect(readSseResponse(response)).resolves.toMatchObject(payload);
  });

  it("SSE error 事件失败关闭", async () => {
    const response = new Response(
      'event: error\ndata: {"error":{"code":"POLICY_DENIED","message":"拒绝","retryable":false}}\n\n',
    );
    await expect(readSseResponse(response)).rejects.toMatchObject({
      code: "POLICY_DENIED",
    });
  });
});
