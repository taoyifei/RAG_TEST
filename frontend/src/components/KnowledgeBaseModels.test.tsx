import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { api, type KnowledgeBaseModelSettings } from "../api/client";
import { KnowledgeBaseModels } from "./KnowledgeBaseModels";

const settings: KnowledgeBaseModelSettings = {
  generation_connection_id: null,
  generation_model: null,
  rewrite_enabled: false,
  ocr_connection_id: null,
  ocr_model: null,
  ocr_enabled: false,
  budget_campaign_id: null,
};
beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "modelSettings").mockResolvedValue(settings);
  vi.spyOn(api, "providerCatalog").mockResolvedValue({
    catalog_version: "test",
    providers: [
      {
        provider_type: "aliyun-model-studio",
        display_name: "合成服务",
        operations: ["generation", "image.ocr"],
        models: ["qwen3.7-flash", "qwen3.5-ocr"],
        regions: [],
        endpoint_profiles: [],
        operation_models: {
          generation: ["qwen3.7-flash"],
          "image.ocr": ["qwen3.5-ocr"],
        },
      },
    ],
  });
  vi.spyOn(api, "listConnections").mockResolvedValue({
    items: [
      {
        connection_id: "conn_test",
        credential_id: "cred_test",
        provider_type: "aliyun-model-studio",
        display_name: "合成连接",
        enabled: true,
        status: "active",
        configuration_version: 1,
        region: "cn-beijing",
        endpoint_mode: "beijing_dashscope",
      },
    ],
  });
});

it("保存配对模型引用，不触发验证或实际问答；清空回答会关闭改写", async () => {
  const user = userEvent.setup();
  const save = vi
    .spyOn(api, "saveModelSettings")
    .mockImplementation((_kb, value) => Promise.resolve(value));
  const validate = vi.spyOn(api, "validateConnection");
  const answer = vi.spyOn(api, "answer");
  render(<KnowledgeBaseModels kbId="kb_test" />);
  expect(await screen.findByText(/未配置，使用证据摘录回答/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "设置回答与图片识别" }));
  expect(await screen.findByText(/已选择模型不等于连接已验证/)).toBeVisible();
  const generation = await screen.findByRole("combobox", { name: "回答模型" });
  await waitFor(() => expect(generation).toBeEnabled());
  await user.selectOptions(
    generation,
    JSON.stringify(["conn_test", "qwen3.7-flash"]),
  );
  await user.click(screen.getByLabelText("启用问题改写"));
  await user.selectOptions(generation, "");
  expect(screen.getByLabelText("启用问题改写")).not.toBeChecked();
  await user.selectOptions(
    generation,
    JSON.stringify(["conn_test", "qwen3.7-flash"]),
  );
  await user.click(screen.getByLabelText("启用问题改写"));
  await user.selectOptions(
    screen.getByLabelText("图片识别模型"),
    JSON.stringify(["conn_test", "qwen3.5-ocr"]),
  );
  await user.click(screen.getByLabelText("启用文档图片识别"));
  await user.click(screen.getByRole("button", { name: "保存设置" }));
  expect(save).toHaveBeenCalledWith("kb_test", {
    ...settings,
    generation_connection_id: "conn_test",
    generation_model: "qwen3.7-flash",
    rewrite_enabled: true,
    ocr_connection_id: "conn_test",
    ocr_model: "qwen3.5-ocr",
    ocr_enabled: true,
  });
  expect(validate).not.toHaveBeenCalled();
  expect(answer).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("设置保存失败保留输入并显示错误", async () => {
  const user = userEvent.setup();
  vi.spyOn(api, "saveModelSettings").mockRejectedValue(new Error("连接已停用"));
  render(<KnowledgeBaseModels kbId="kb_test" />);
  await user.click(
    await screen.findByRole("button", { name: "设置回答与图片识别" }),
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "保存设置" })).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: "保存设置" }));
  expect(await screen.findByText("连接已停用")).toBeVisible();
  expect(screen.getByRole("dialog")).toBeVisible();
});
