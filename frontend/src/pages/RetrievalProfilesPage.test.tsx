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
          },
          {
            provider_type: "aliyun-model-studio",
            models: ["qwen3.7-text-embedding"],
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
