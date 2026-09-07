import { api, type RetrievalProfile } from "../api/client";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { ConsoleProvider } from "../state/console-context";
import { RetrievalProfilesPage } from "./RetrievalProfilesPage";

afterEach(() => {
  vi.restoreAllMocks();
  sessionStorage.clear();
});

it("只向 Qwen 发送自定义指令，并回显保存后的实际参数", async () => {
  sessionStorage.setItem(
    "rag.console.scope",
    JSON.stringify({ projectId: "prj_1", kbId: "kb_1", revisionId: "" }),
  );
  const bodies: Record<string, unknown>[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    let body: object = { items: [] };
    if (url.includes("provider-catalog"))
      body = {
        providers: [
          {
            provider_type: "jina",
            models: ["jina-embeddings-v5-text-small", "jina-reranker-v3.5"],
            operation_models: {
              "embedding.document": ["jina-embeddings-v5-text-small"],
              "embedding.query": ["jina-embeddings-v5-text-small"],
              reranking: ["jina-reranker-v3.5"],
            },
          },
          {
            provider_type: "aliyun-model-studio",
            models: ["qwen3.7-text-embedding"],
            operation_models: {
              "embedding.document": ["qwen3.7-text-embedding"],
              "embedding.query": ["qwen3.7-text-embedding"],
            },
          },
        ],
      };
    if (url.endsWith("provider-connections"))
      body = {
        items: [
          {
            connection_id: "conn_jina",
            provider_type: "jina",
            display_name: "合成 Jina",
          },
          {
            connection_id: "conn_aliyun",
            provider_type: "aliyun-model-studio",
            display_name: "合成 Qwen",
            request_budget: 4,
            token_budget: 2048,
          },
        ],
      };
    if (url.endsWith("retrieval-profiles") && init?.method === "POST") {
      if (typeof init.body !== "string") throw new Error("预期 JSON 请求体");
      const payload = JSON.parse(init.body) as Record<string, unknown>;
      bodies.push(payload);
      body = {
        ...payload,
        profile_revision_id: "pfr_synthetic",
        status: "draft",
      };
    }
    if (url.endsWith(":preview"))
      body = {
        impact: "NEW_INDEX_REVISION_REQUIRED",
        index_fingerprint_changed: true,
        serving_fingerprint_changed: true,
      };
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status: url.endsWith("console/session") ? 401 : 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });
  const user = userEvent.setup();
  render(
    <ConsoleProvider>
      <RetrievalProfilesPage />
    </ConsoleProvider>,
  );
  await screen.findByRole("option", { name: "合成 Jina" });
  expect(screen.queryByRole("textbox", { name: "Qwen 查询指令" })).toBeNull();
  await user.selectOptions(screen.getByLabelText("主向量连接"), "conn_jina");
  await user.selectOptions(
    screen.getByLabelText("备用向量连接"),
    "conn_aliyun",
  );
  await user.click(screen.getByText("高级设置"));
  for (const [index, instruction] of [
    "检索设备维护资料",
    "检索采购合同资料",
  ].entries()) {
    await user.clear(screen.getByLabelText("Qwen 查询指令"));
    await user.type(screen.getByLabelText("Qwen 查询指令"), instruction);
    await user.click(screen.getByRole("button", { name: "创建并预览影响" }));
    await waitFor(() => expect(bodies).toHaveLength(index + 1));
    expect(bodies[index].standby_query_policy).toEqual({
      text_type: "query",
      query_instruct: instruction,
    });
    expect(bodies[index].primary_query_policy).toEqual({
      task: "retrieval.query",
      normalized: true,
    });
    expect(
      await screen.findByText(`已解析 Qwen 指令：${instruction}`),
    ).toBeVisible();
  }
});

it("编辑既有方案保留权威证据策略、查询参数和备用预算", async () => {
  sessionStorage.setItem(
    "rag.console.scope",
    JSON.stringify({
      projectId: "prj_one",
      kbId: "kb_one",
      revisionId: "irev_one",
    }),
  );
  const profile: RetrievalProfile = {
    profile_revision_id: "pfr_existing",
    knowledge_base_id: "kb_one",
    status: "active",
    primary_connection_id: "conn_jina",
    primary_embedding_model: "jina-embeddings-v5-text-small",
    primary_dimension: 1024,
    primary_document_policy: { task: "retrieval.passage", normalized: true },
    primary_query_policy: { task: "retrieval.query", normalized: true },
    standby_connection_id: "conn_aliyun",
    standby_embedding_model: "qwen3.7-text-embedding",
    standby_dimension: 1024,
    standby_document_policy: { text_type: "document" },
    standby_query_policy: {
      text_type: "query",
      query_instruct: "既有查询指令",
    },
    reranker_connection_id: null,
    reranker_model: null,
    failover_enabled: true,
    standby_budget: { requests: 3, tokens: 8192 },
    retrieval_policy: {
      rrf_k: 72,
      minimum_support_items: 2,
      max_evidence_items: 9,
      evidence_token_budget: 2048,
    },
    evidence_policy: { minimum_units: 2 },
    index_semantic_fingerprint: "synthetic-index",
    serving_fingerprint: "synthetic-serving",
  };
  vi.spyOn(api, "resumeSession").mockRejectedValue(
    new Error("synthetic no session"),
  );
  vi.spyOn(api, "listConnections").mockResolvedValue({
    items: [
      {
        connection_id: "conn_jina",
        credential_id: "cred_jina",
        display_name: "合成 Jina",
        provider_type: "jina",
        configuration_version: 1,
        status: "configured",
      },
      {
        connection_id: "conn_aliyun",
        credential_id: "cred_aliyun",
        display_name: "合成 Qwen",
        provider_type: "aliyun-model-studio",
        configuration_version: 1,
        status: "configured",
        request_budget: 5,
        token_budget: 10000,
      },
    ],
  });
  vi.spyOn(api, "providerCatalog").mockResolvedValue({
    catalog_version: "synthetic",
    providers: [
      {
        provider_type: "jina",
        display_name: "Jina",
        models: ["jina-embeddings-v5-text-small", "jina-reranker-v3.5"],
        operations: ["embedding.document", "embedding.query", "reranking"],
        operation_models: {
          "embedding.document": ["jina-embeddings-v5-text-small"],
          "embedding.query": ["jina-embeddings-v5-text-small"],
          reranking: ["jina-reranker-v3.5"],
        },
        regions: [],
        endpoint_profiles: [],
      },
      {
        provider_type: "aliyun-model-studio",
        display_name: "百炼",
        models: ["qwen3.7-text-embedding"],
        operations: ["embedding.document", "embedding.query"],
        operation_models: {
          "embedding.document": ["qwen3.7-text-embedding"],
          "embedding.query": ["qwen3.7-text-embedding"],
        },
        regions: ["cn-beijing"],
        endpoint_profiles: [],
      },
    ],
  });
  vi.spyOn(api, "listRetrievalProfiles").mockResolvedValue({
    items: [profile],
  });
  const create = vi
    .spyOn(api, "createRetrievalProfile")
    .mockResolvedValue({
      ...profile,
      profile_revision_id: "pfr_next",
      status: "draft",
    });
  vi.spyOn(api, "previewRetrievalProfile").mockResolvedValue({
    impact: "NO_REINDEX",
    proposed_profile_revision_id: "pfr_next",
    index_fingerprint_changed: false,
    serving_fingerprint_changed: false,
  });
  const user = userEvent.setup();
  render(
    <ConsoleProvider>
      <RetrievalProfilesPage />
    </ConsoleProvider>,
  );
  await user.click(await screen.findByRole("button", { name: "编辑方案" }));
  expect(screen.getByLabelText("备用请求预算")).toHaveValue(3);
  expect(screen.getByLabelText("备用 Token 预算")).toHaveValue(8192);
  await user.click(screen.getByRole("button", { name: "创建并预览影响" }));
  await waitFor(() => expect(create).toHaveBeenCalledOnce());
  expect(create).toHaveBeenCalledWith(
    "kb_one",
    expect.objectContaining({
      evidence_policy: profile.evidence_policy,
      retrieval_policy: profile.retrieval_policy,
      standby_budget: profile.standby_budget,
      primary_query_policy: profile.primary_query_policy,
      standby_query_policy: profile.standby_query_policy,
      reranker_connection_id: null,
      reranker_model: null,
    }),
  );
  expect(await screen.findByRole("button", { name: "保存设置" })).toBeVisible();
});
