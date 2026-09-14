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
    catalog_version: "prior-catalog",
    validation_mode: "mock",
    request_policy_identity: "synthetic-policy",
    is_current: true,
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

it("自定义连接按能力接受自由模型 ID，并把向量维度带入真实测试", async () => {
  const user = userEvent.setup();
  const connection: ProviderConnection = {
    connection_id: "conn_custom",
    credential_id: "cred_custom",
    configuration_version: 1,
    display_name: "内部兼容服务",
    provider_type: "openai-compatible",
    status: "configured",
    endpoint_mode: "custom",
    api_base_url: "http://127.0.0.1:18080/v1",
    rerank_protocol: "tei",
    rerank_path: "/rerank",
  };
  vi.spyOn(api, "providerCatalog").mockResolvedValue({
    catalog_version: "synthetic",
    providers: [
      {
        provider_type: "openai-compatible",
        display_name: "OpenAI-compatible",
        models: [],
        operations: [
          "embedding.document",
          "embedding.query",
          "reranking",
          "generation",
          "query.interpret",
          "query.rewrite",
        ],
        operation_models: {},
        regions: [],
        endpoint_profiles: ["default"],
      },
    ],
  });
  vi.spyOn(api, "listCredentials").mockResolvedValue({
    items: [
      {
        credential_id: "cred_custom",
        provider_type: "openai-compatible",
        configured: true,
        source: "database_encrypted",
        masked_hint: "未配置（无鉴权）",
        key_version: 1,
        status: "active",
      },
    ],
  });
  vi.spyOn(api, "listConnections").mockResolvedValue({ items: [connection] });
  vi.spyOn(api, "listDailyProviderUsage").mockResolvedValue({ items: [] });
  vi.spyOn(api, "listValidations").mockResolvedValue({ items: [] });
  const validate = vi.spyOn(api, "validateConnection").mockResolvedValue({
    validation_id: "val_custom",
    connection_id: "conn_custom",
    operation: "embedding.query",
    provider_model: "vendor/embed-v2",
    status: "succeeded",
    http_category: "http_200",
    dimension: 3072,
    finished_at: "2026-09-14T00:00:00Z",
    configuration_version: 1,
    credential_key_version: 1,
    catalog_version: "synthetic",
    validation_mode: "live",
    request_policy_identity: "custom-policy",
  });

  render(<ModelServicesPage />);
  const card = (await screen.findByText("内部兼容服务")).closest("article")!;
  expect(
    within(card).getByText(/Base URL：http:\/\/127\.0\.0\.1/),
  ).toBeVisible();
  expect(within(card).getByLabelText("回答生成模型 ID")).toBeVisible();
  expect(within(card).getByLabelText("问题意图解释模型 ID")).toBeVisible();
  expect(within(card).getByLabelText("问题改写模型 ID")).toBeVisible();
  await user.type(
    within(card).getByLabelText("查询向量模型 ID"),
    "vendor/embed-v2",
  );
  await user.type(
    within(card).getAllByLabelText("期望 Embedding 维度")[1],
    "3072",
  );
  await user.click(within(card).getByRole("button", { name: "测试查询向量" }));
  await user.click(screen.getByRole("button", { name: "开始测试" }));
  expect(validate).toHaveBeenCalledWith("conn_custom", {
    operation: "embedding.query",
    model: "vendor/embed-v2",
    expected_dimension: 3072,
  });
});
