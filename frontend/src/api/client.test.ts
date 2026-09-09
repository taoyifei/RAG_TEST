import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, readSseResponse, setBrowserCsrfToken } from "./client";

afterEach(() => {
  setBrowserCsrfToken("");
  vi.restoreAllMocks();
});

describe("API 客户端契约", () => {
  it("删除正确处理204空响应与202接受状态，并携带会话CSRF", async () => {
    setBrowserCsrfToken("synthetic-csrf");
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ status: "deleting" }), { status: 202 }),
      );
    await expect(api.deleteDocument("", "prj", "kb", "doc")).resolves.toEqual({
      statusCode: 204,
      document: undefined,
    });
    await expect(api.deleteDocument("", "prj", "kb", "doc")).resolves.toEqual({
      statusCode: 202,
      document: { status: "deleting" },
    });
    expect(
      new Headers(fetchMock.mock.calls[0][1]?.headers).get("X-CSRF-Token"),
    ).toBe("synthetic-csrf");
    expect(fetchMock.mock.calls[0][1]?.credentials).toBe("same-origin");
  });

  it("历史关键词参数化且正文保存选择只作用于该次请求", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() => Promise.resolve(new Response("{}")));
    await api.listHistory({ keyword: "职责&status=FAILED", offset: 20 });
    const input = fetchMock.mock.calls[0][0];
    const path =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    const params = new URL(path, "http://localhost").searchParams;
    expect(params.get("keyword")).toBe("职责&status=FAILED");
    expect(params.has("status")).toBe(false);
    await api.answer(
      "",
      "prj",
      "kb",
      "合成问题",
      undefined,
      true,
      "metadata_only",
    );
    const body = JSON.parse(fetchMock.mock.calls[1][1]?.body as string) as {
      history_mode: string;
    };
    expect(body.history_mode).toBe("metadata_only");
  });
  it("相关内容开关保留旧请求形状，每次调用只发送一次请求", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() =>
        Promise.resolve(
          new Response(JSON.stringify({ answer: null, evidence: [] })),
        ),
      );
    await api.answer("query", "prj", "kb", "测试");
    await api.answer("query", "prj", "kb", "测试", undefined, true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(JSON.parse(fetchMock.mock.calls[0][1]?.body as string)).toEqual({
      query: "测试",
      limit: 10,
      stream: false,
    });
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual({
      query: "测试",
      limit: 10,
      stream: false,
      include_related_content: true,
    });
  });
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

  it("增量解析 UTF-8 分片、多行 data、CRLF 与未知合法扩展", async () => {
    const trace = `trace_${"1".repeat(32)}`;
    const project = `prj_${"2".repeat(32)}`;
    const kb = `kb_${"3".repeat(32)}`;
    const common = {
      protocol: "rag-answer-sse-v1",
      trace_id: trace,
      project_id: project,
      knowledge_base_id: kb,
    };
    const frames = [
      `event: meta\r\ndata: ${JSON.stringify({ ...common, type: "meta", sequence: 0, delivery: "incremental_or_final_only" })}\r\n\r\n`,
      `event: stage\r\ndata: {"protocol":"rag-answer-sse-v1",\r\ndata: "trace_id":"${trace}","project_id":"${project}","knowledge_base_id":"${kb}","type":"stage","sequence":1,"stage":"retrieval"}\r\n\r\n`,
      `event: vendor-extension\ndata: ${JSON.stringify({ ...common, type: "vendor-extension", sequence: 2 })}\n\n`,
      `event: claim\ndata: ${JSON.stringify({
        ...common,
        type: "claim",
        sequence: 3,
        claim_index: 0,
        active_index_revision_id: `irev_${"4".repeat(32)}`,
        provisional: true,
        claim: {
          text: "资料员每周核对清单。",
          supports: [{ support_id: "S1", quote: "每周核对清单" }],
        },
      })}\n\n`,
      `event: final\ndata: ${JSON.stringify({
        ...common,
        type: "final",
        sequence: 4,
        status: "ANSWERABLE",
        reason_code: "ANSWERABLE",
        answer: "资料员每周核对清单。 [S1]",
        evidence: [],
        active_index_revision_id: `irev_${"4".repeat(32)}`,
      })}\n\n`,
    ].join("");
    const encoded = new TextEncoder().encode(frames);
    const chinese = new TextEncoder().encode("资");
    const split = encoded.findIndex(
      (_, index) =>
        encoded[index] === chinese[0] && encoded[index + 1] === chinese[1],
    );
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoded.slice(0, split + 1));
        controller.enqueue(encoded.slice(split + 1));
        controller.close();
      },
    });
    const stages: string[] = [];
    const claims: string[] = [];
    const response = new Response(body, {
      headers: { "X-Trace-Id": trace },
    });

    await expect(
      readSseResponse(
        response,
        {
          onStage: (stage) => stages.push(stage),
          onClaim: (claim) => claims.push(claim.text),
        },
        { projectId: project, knowledgeBaseId: kb },
      ),
    ).resolves.toMatchObject({ answer: "资料员每周核对清单。 [S1]" });
    expect(stages).toEqual(["retrieval"]);
    expect(claims).toEqual(["资料员每周核对清单。"]);
  });

  it("拒绝跨知识库或不连续序号的流事件", async () => {
    const trace = `trace_${"1".repeat(32)}`;
    const response = new Response(
      `event: meta\ndata: ${JSON.stringify({
        protocol: "rag-answer-sse-v1",
        type: "meta",
        sequence: 1,
        trace_id: trace,
        project_id: `prj_${"2".repeat(32)}`,
        knowledge_base_id: `kb_${"3".repeat(32)}`,
      })}\n\n`,
      { headers: { "X-Trace-Id": trace } },
    );
    await expect(
      readSseResponse(response, {}, {
        projectId: `prj_${"2".repeat(32)}`,
        knowledgeBaseId: `kb_${"9".repeat(32)}`,
      }),
    ).rejects.toThrow(/序号|范围/);
  });

  it("协商新协议后拒绝降级事件和名称类型错配", async () => {
    const project = `prj_${"2".repeat(32)}`;
    const kb = `kb_${"3".repeat(32)}`;
    const scope = { projectId: project, knowledgeBaseId: kb };
    await expect(
      readSseResponse(
        new Response('event: meta\ndata: {"trace_id":"trace_legacy"}\n\n'),
        {},
        scope,
      ),
    ).rejects.toThrow(/协议版本/);

    const trace = `trace_${"1".repeat(32)}`;
    await expect(
      readSseResponse(
        new Response(
          `event: meta\ndata: ${JSON.stringify({
            protocol: "rag-answer-sse-v1",
            type: "stage",
            sequence: 0,
            trace_id: trace,
            project_id: project,
            knowledge_base_id: kb,
          })}\n\n`,
          { headers: { "X-Trace-Id": trace } },
        ),
        {},
        scope,
      ),
    ).rejects.toThrow(/名称与类型/);
  });

  it("协议事件校验失败时主动取消响应读取，不让上游继续计费", async () => {
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'event: meta\ndata: {"trace_id":"legacy"}\n\n',
          ),
        );
      },
      cancel() {
        cancelled = true;
      },
    });
    await expect(
      readSseResponse(new Response(body), {}, {
        projectId: `prj_${"2".repeat(32)}`,
        knowledgeBaseId: `kb_${"3".repeat(32)}`,
      }),
    ).rejects.toThrow(/协议版本/);
    expect(cancelled).toBe(true);
  });

  it("Final 是唯一终态，拒绝其后的扩展或错误事件", async () => {
    const trace = `trace_${"1".repeat(32)}`;
    const project = `prj_${"2".repeat(32)}`;
    const kb = `kb_${"3".repeat(32)}`;
    const common = {
      protocol: "rag-answer-sse-v1",
      trace_id: trace,
      project_id: project,
      knowledge_base_id: kb,
    };
    const response = new Response(
      `event: final\ndata: ${JSON.stringify({
        ...common,
        type: "final",
        sequence: 0,
        status: "INSUFFICIENT_EVIDENCE",
        reason_code: "INSUFFICIENT_SUPPORT",
        answer: null,
        evidence: [],
        active_index_revision_id: `irev_${"4".repeat(32)}`,
      })}\n\nevent: vendor-extension\ndata: ${JSON.stringify({
        ...common,
        type: "vendor-extension",
        sequence: 1,
      })}\n\n`,
      { headers: { "X-Trace-Id": trace } },
    );
    await expect(
      readSseResponse(response, {}, {
        projectId: project,
        knowledgeBaseId: kb,
      }),
    ).rejects.toThrow(/Final 后/);
  });

  it("默认使用同源 Cookie 与 CSRF，不再发送浏览器 Bearer Token", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ project_id: "prj_1" }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    setBrowserCsrfToken("synthetic-csrf");
    await api.createProject("must-not-be-sent", "测试", "project-key");

    const init = fetchMock.mock.calls[0]?.[1];
    const headers = new Headers(init?.headers);
    expect(init?.credentials).toBe("same-origin");
    expect(headers.get("Authorization")).toBeNull();
    expect(headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  });

  it("页面托管密钥不写入浏览器 Storage", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ credential_id: "cred_1" }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    const credentialValue = "browser-storage-synthetic-value";
    await api.createCredential({
      provider_type: "jina",
      source: "database_encrypted",
      secret_value: credentialValue,
    });

    expect(localStorage.getItem(credentialValue)).toBeNull();
    expect(sessionStorage.getItem(credentialValue)).toBeNull();
    expect(JSON.stringify(localStorage)).not.toContain(credentialValue);
    expect(JSON.stringify(sessionStorage)).not.toContain(credentialValue);
  });
});
