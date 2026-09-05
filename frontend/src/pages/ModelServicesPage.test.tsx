import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import {
  api,
  type ProviderConnection,
  type ProviderValidation,
} from "../api/client";
import { ModelServicesPage } from "./ModelServicesPage";

afterEach(() => vi.restoreAllMocks());

it("同服务商两条连接逐项隔离，配置变化后的成功记录必须重测", async () => {
  const user = userEvent.setup();
  const first: ProviderConnection = {
    connection_id: "conn_one",
    credential_id: "cred_one",
    configuration_version: 1,
    display_name: "百炼一",
    provider_type: "aliyun-model-studio",
    status: "configured",
    endpoint_mode: "beijing_dashscope",
  };
  const second = {
    ...first,
    connection_id: "conn_two",
    display_name: "百炼二",
  };
  vi.spyOn(api, "providerCatalog").mockResolvedValue({
    catalog_version: "synthetic",
    providers: [
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
  vi.spyOn(api, "listCredentials").mockResolvedValue({
    items: [
      {
        credential_id: "cred_one",
        provider_type: "aliyun-model-studio",
        configured: true,
        source: "database_encrypted",
        masked_hint: "••••demo",
        key_version: 1,
        status: "active",
      },
    ],
  });
  vi.spyOn(api, "listDailyProviderUsage").mockResolvedValue({ items: [] });
  const connections = vi
    .spyOn(api, "listConnections")
    .mockResolvedValue({ items: [first, second] });
  const run: ProviderValidation = {
    validation_id: "val_one",
    connection_id: "conn_one",
    operation: "embedding.query",
    provider_model: "qwen3.7-text-embedding",
    status: "succeeded",
    http_category: "mock_200",
    finished_at: "2026-09-05T00:00:00Z",
    configuration_version: 1,
    credential_key_version: 1,
    catalog_version: "synthetic",
    validation_mode: "mock",
    request_policy_identity: "synthetic-policy",
  };
  vi.spyOn(api, "listValidations").mockImplementation((id) =>
    Promise.resolve({
      items: id === "conn_one" ? [run] : [],
    }),
  );
  const probe = vi.spyOn(api, "validateConnection");
  render(<ModelServicesPage />);
  const one = (await screen.findByText("百炼一")).closest("article")!;
  const two = screen.getByText("百炼二").closest("article")!;
  expect(within(one).getAllByText("离线模拟通过")).toHaveLength(1);
  expect(within(one).getByText("尚未验证")).toBeVisible();
  expect(within(two).queryByText("离线模拟通过")).toBeNull();
  expect(within(two).getAllByText("尚未验证")).toHaveLength(2);
  connections.mockResolvedValue({
    items: [{ ...first, configuration_version: 2 }, second],
  });
  await user.click(screen.getByRole("button", { name: "刷新" }));
  expect(await within(one).findByText("配置已变化需重新测试")).toBeVisible();
  expect(within(one).queryByText("离线模拟通过")).toBeNull();
  expect(probe).not.toHaveBeenCalled();
});
