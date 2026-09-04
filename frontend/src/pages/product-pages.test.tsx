import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConsoleProvider } from "../state/console-context";
import { AccessTokensPage } from "./AccessTokensPage";
import { FirstRunWizard } from "./FirstRunWizard";
import { ModelServicesPage } from "./ModelServicesPage";
import { RetrievalProfilesPage } from "./RetrievalProfilesPage";

function jsonResponse(value: object, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function requestPath(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

const providerCatalog = {
  catalog_version: "test-catalog",
  providers: [
    {
      provider_type: "jina",
      display_name: "Jina",
      operations: ["embedding.document", "embedding.query", "reranking"],
      models: ["jina-embeddings-v5-text-small", "jina-reranker-v3.5"],
      regions: [],
      endpoint_profiles: ["default"],
    },
    {
      provider_type: "aliyun-model-studio",
      display_name: "阿里云百炼",
      operations: ["embedding.document", "embedding.query"],
      models: ["qwen3.7-text-embedding"],
      regions: ["cn-beijing"],
      endpoint_profiles: ["default"],
    },
  ],
};

afterEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("产品页面", () => {
  it("首次使用向导只用管理口令交换 Cookie 会话", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (requestPath(input).endsWith("/api/v1/console/session")) {
        return Promise.resolve(
          jsonResponse({
            authenticated: true,
            session_id: "sess_1",
            csrf_token: "csrf_1",
            expires_in: 3600,
          }),
        );
      }
      return Promise.resolve(jsonResponse({}, 401));
    });
    render(
      <ConsoleProvider>
        <FirstRunWizard open onClose={() => undefined} />
      </ConsoleProvider>,
    );
    const bootstrapValue = "browser-bootstrap-synthetic-value";
    await user.type(screen.getByLabelText("管理口令"), bootstrapValue);
    await user.click(screen.getByRole("button", { name: "进入工作台" }));

    expect(screen.getByLabelText("管理口令")).toHaveValue("");
    expect(JSON.stringify(localStorage)).not.toContain(bootstrapValue);
    expect(JSON.stringify(sessionStorage)).not.toContain(bootstrapValue);
  });

  it("模型服务页面提供固定服务商和连接验证入口", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (requestPath(input).includes("provider-catalog")) {
        return Promise.resolve(jsonResponse(providerCatalog));
      }
      if (requestPath(input).includes("provider-credentials")) {
        return Promise.resolve(jsonResponse({ items: [] }));
      }
      return Promise.resolve(jsonResponse({ items: [] }));
    });
    render(<ModelServicesPage />);

    expect(await screen.findByRole("heading", { name: "模型服务" })).toBeVisible();
    expect(screen.getByRole("option", { name: "Jina" })).toBeVisible();
    expect(screen.getByRole("option", { name: "阿里云百炼" })).toBeVisible();
    expect(screen.getByText("尚未配置模型服务")).toBeVisible();
  });

  it("检索方案与接口访问页面遵循当前知识库范围", async () => {
    sessionStorage.setItem(
      "rag.console.scope",
      JSON.stringify({ projectId: "prj_1", kbId: "kb_1", revisionId: "" }),
    );
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const path = requestPath(input);
      if (path.endsWith("/api/v1/console/session")) {
        return Promise.resolve(jsonResponse({}, 401));
      }
      if (path.includes("provider-catalog")) {
        return Promise.resolve(jsonResponse(providerCatalog));
      }
      return Promise.resolve(jsonResponse({ items: [] }));
    });
    const { rerender } = render(
      <ConsoleProvider>
        <RetrievalProfilesPage />
      </ConsoleProvider>,
    );
    expect(
      await screen.findByRole("heading", { name: "检索方案" }),
    ).toBeVisible();

    rerender(
      <ConsoleProvider>
        <AccessTokensPage />
      </ConsoleProvider>,
    );
    expect(
      await screen.findByRole("heading", { name: "接口访问" }),
    ).toBeVisible();
  });
});
