import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { api, type ProviderConnection } from "../api/client";
import { ConnectionEditor } from "./ConnectionEditor";
import { ModelServicesPage } from "./ModelServicesPage";

const connection: ProviderConnection = {
  connection_id: "conn_synthetic",
  credential_id: "cred_saved",
  display_name: "百炼测试",
  provider_type: "aliyun-model-studio",
  status: "degraded",
  configuration_version: 3,
  workspace_id: "ws-demo000000001",
  endpoint_mode: "workspace_host",
  api_host: "https://old.cn-beijing.maas.aliyuncs.com",
};

afterEach(() => vi.restoreAllMocks());

it("保存前展示裸主机的规范结果，保存保留模式与凭据", async () => {
  const user = userEvent.setup();
  const update = vi
    .spyOn(api, "updateConnection")
    .mockResolvedValue(connection);
  const validate = vi.spyOn(api, "validateConnection");
  const rotate = vi.spyOn(api, "rotateCredential");
  render(
    <ConnectionEditor
      connection={connection}
      onSaved={() => Promise.resolve()}
      onCancel={() => undefined}
    />,
  );
  await user.clear(screen.getByLabelText("API Host"));
  await user.type(
    screen.getByLabelText("API Host"),
    "api-synthetic.cn-beijing.maas.aliyuncs.com:443/",
  );
  expect(screen.getByRole("status")).toHaveTextContent(
    "保存前规范结果：https://api-synthetic.cn-beijing.maas.aliyuncs.com",
  );
  expect(update).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "保存修改" }));
  expect(update).toHaveBeenCalledWith(
    "conn_synthetic",
    expect.objectContaining({
      endpoint_mode: "workspace_host",
      api_host: "https://api-synthetic.cn-beijing.maas.aliyuncs.com",
    }),
  );
  expect(JSON.stringify(update.mock.calls)).not.toContain("credential");
  expect(validate).not.toHaveBeenCalled();
  expect(rotate).not.toHaveBeenCalled();
});

it("历史缺失模式保持未选择且非法 Host 无法保存", async () => {
  const user = userEvent.setup();
  const update = vi.spyOn(api, "updateConnection");
  render(
    <ConnectionEditor
      connection={{ ...connection, endpoint_mode: "", api_host: "" }}
      onSaved={() => Promise.resolve()}
      onCancel={() => undefined}
    />,
  );
  expect(screen.getByLabelText("端点模式")).toHaveValue("");
  await user.click(screen.getByRole("button", { name: "保存修改" }));
  expect(update).not.toHaveBeenCalled();
  await user.selectOptions(screen.getByLabelText("端点模式"), "workspace_host");
  await user.type(screen.getByLabelText("API Host"), "https://evil.invalid");
  await user.click(screen.getByRole("button", { name: "保存修改" }));
  expect(update).not.toHaveBeenCalled();
});

it("原地编辑保留凭据且不触发付费测试", async () => {
  const user = userEvent.setup();
  const update = vi
    .spyOn(api, "updateConnection")
    .mockResolvedValue({ ...connection, configuration_version: 4 });
  const validate = vi.spyOn(api, "validateConnection");
  const createCredential = vi.spyOn(api, "createCredential");
  const saved = vi.fn().mockResolvedValue(undefined);
  render(
    <ConnectionEditor
      connection={connection}
      onSaved={saved}
      onCancel={() => undefined}
    />,
  );
  expect(screen.getByLabelText("工作空间标识")).toHaveValue("ws-demo000000001");
  expect(screen.getByText(/密钥已保存，修改连接无需重新填写/)).toBeVisible();
  await user.selectOptions(
    screen.getByLabelText("端点模式"),
    "beijing_dashscope",
  );
  await user.click(screen.getByRole("button", { name: "保存修改" }));
  expect(update).toHaveBeenCalledWith(
    "conn_synthetic",
    expect.objectContaining({
      expected_version: 3,
      endpoint_mode: "beijing_dashscope",
      api_host: "https://dashscope.aliyuncs.com",
    }),
  );
  expect(JSON.stringify(update.mock.calls)).not.toContain("secret");
  expect(createCredential).not.toHaveBeenCalled();
  expect(validate).not.toHaveBeenCalled();
  expect(saved).toHaveBeenCalledOnce();
});

it("版本冲突保留编辑表单", async () => {
  const user = userEvent.setup();
  vi.spyOn(api, "updateConnection").mockRejectedValue(
    new Error("连接已被修改，请刷新后重试"),
  );
  const saved = vi.fn();
  render(
    <ConnectionEditor
      connection={connection}
      onSaved={saved}
      onCancel={() => undefined}
    />,
  );
  await user.click(screen.getByRole("button", { name: "保存修改" }));
  expect(screen.getByLabelText("API Host")).toHaveValue(connection.api_host);
  expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
  expect(saved).not.toHaveBeenCalled();
});

it("单项测试在途禁用重复点击，失败后仍可编辑", async () => {
  const user = userEvent.setup();
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
        endpoint_profiles: ["default"],
      },
    ],
  });
  vi.spyOn(api, "listCredentials").mockResolvedValue({ items: [] });
  vi.spyOn(api, "listConnections").mockResolvedValue({ items: [connection] });
  vi.spyOn(api, "listValidations").mockResolvedValue({ items: [] });
  vi.spyOn(api, "listDailyProviderUsage").mockResolvedValue({ items: [] });
  let rejectProbe: (reason: Error) => void = () => undefined;
  const validate = vi.spyOn(api, "validateConnection").mockImplementation(
    () =>
      new Promise((_resolve, reject) => {
        rejectProbe = reject;
      }),
  );
  render(<ModelServicesPage />);
  const button = await screen.findByRole("button", { name: "测试文档向量" });
  await user.click(button);
  expect(validate).not.toHaveBeenCalled();
  await user.dblClick(screen.getByRole("button", { name: "开始测试" }));
  expect(validate).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "测试中…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "测试查询向量" })).toBeEnabled();
  rejectProbe(new Error("synthetic failure"));
  expect(
    await screen.findByRole("button", { name: "测试文档向量" }),
  ).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "编辑连接" }));
  const editor = screen.getByRole("dialog", { name: "编辑百炼连接" });
  expect(within(editor).getByLabelText("工作空间标识")).toHaveValue(
    connection.workspace_id,
  );
});

it("取消不会写入或测试，轮换失败清空密钥且保留连接字段", async () => {
  const user = userEvent.setup();
  const update = vi.spyOn(api, "updateConnection");
  const validate = vi.spyOn(api, "validateConnection");
  const cancel = vi.fn();
  const rotate = vi
    .spyOn(api, "rotateCredential")
    .mockRejectedValue(new Error("synthetic failure"));
  render(
    <ConnectionEditor
      connection={connection}
      credential={{
        credential_id: "cred_saved",
        provider_type: "aliyun-model-studio",
        configured: true,
        source: "database_encrypted",
        masked_hint: "••••demo",
        key_version: 1,
        status: "active",
      }}
      onSaved={() => Promise.resolve()}
      onCancel={cancel}
    />,
  );
  await user.click(screen.getByRole("button", { name: "更换密钥" }));
  await user.type(
    screen.getByLabelText("新服务密钥"),
    "synthetic-rotation-value",
  );
  await user.click(screen.getByRole("button", { name: "确认更换密钥" }));
  expect(rotate).toHaveBeenCalledOnce();
  expect(screen.getByLabelText("新服务密钥")).toHaveValue("");
  expect(screen.getByLabelText("API Host")).toHaveValue(connection.api_host);
  await user.click(screen.getByRole("button", { name: "取消" }));
  expect(cancel).toHaveBeenCalledOnce();
  expect(update).not.toHaveBeenCalled();
  expect(validate).not.toHaveBeenCalled();
});
