import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import {
  api,
  type CorpusAuthorizationStatus,
  type KnowledgeBaseModelSettings,
} from "../api/client";
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

it("明确区分本地确定性检索，并由管理员一次批准当前活动资料", async () => {
  const user = userEvent.setup();
  const missing: CorpusAuthorizationStatus = {
    corpus_authorization_state: "MISSING",
    model_configuration_state: "CONFIGURED",
    model_authorization_state: "MISSING",
    budget_state: "MISSING",
    required_operations: ["generation", "query.interpret", "query.rewrite"],
    pending_operations: [],
    fallback_reason_codes: ["CORPUS_AUTHORIZATION_MISSING"],
  };
  vi.spyOn(api, "modelSettings").mockResolvedValue({
    ...settings,
    generation_connection_id: "conn_test",
    generation_model: "qwen3.7-flash",
    rewrite_enabled: true,
    corpus_authorization: missing,
    retrieval_data_plane: {
      retrieval_data_plane: "default_local_fallback",
      profile_state: "NOT_CONFIGURED",
      active_index_revision_id: "irev_test",
      embedding_provider_id: "deterministic",
      embedding_model: "deterministic-sha256-v1",
      reranker_provider_id: "lexical_overlap",
      reranker_model: "1",
      dense_calibration_state: "UNCALIBRATED",
      vector_coverage_complete: true,
      fallback_reason_codes: ["NO_ACTIVE_RETRIEVAL_PROFILE"],
      remediation_path: "/retrieval-profiles",
    },
  });
  const approved: CorpusAuthorizationStatus = {
    ...missing,
    corpus_authorization_state: "APPROVED",
    model_authorization_state: "APPROVED",
    budget_state: "AVAILABLE",
    fallback_reason_codes: [],
    manifest: {
      manifest_id: `cauth_${"a".repeat(32)}`,
      project_id: `prj_${"b".repeat(32)}`,
      knowledge_base_id: `kb_${"c".repeat(32)}`,
      active_index_revision_id: `irev_${"d".repeat(32)}`,
      active_document_digest: `sha256:${"e".repeat(64)}`,
      active_document_count: 2,
      provider_connection_id: "conn_test",
      provider_model: "qwen3.7-flash",
      operation_binding_identity: `sha256:${"f".repeat(64)}`,
      operations: ["generation", "query.interpret", "query.rewrite"],
      authorization_id: "synthetic-authorization",
      budget_campaign_id: "synthetic-budget",
      created_at: "2030-01-01T00:00:00+00:00",
      expires_at: "2030-02-01T00:00:00+00:00",
      policy_revision: "corpus-authorization-v1",
      approved_by_session_id: "sess_test",
    },
  };
  const approve = vi
    .spyOn(api, "approveCorpusAuthorization")
    .mockResolvedValue(approved);

  render(<KnowledgeBaseModels kbId="kb_test" />);
  expect(await screen.findByText(/当前检索：本地确定性检索/)).toBeVisible();
  expect(screen.getByText(/尚未批准当前活动语料/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "批准当前活动资料" }));
  expect(
    screen.getByText(/本次用途：回答生成、问题意图解释、问题改写/),
  ).toBeVisible();
  await user.click(screen.getByRole("button", { name: "确认批准当前版本" }));
  await waitFor(() => expect(approve).toHaveBeenCalledTimes(1));
  expect(approve.mock.calls[0][0]).toBe("kb_test");
  expect(approve.mock.calls[0][1]).toEqual(
    expect.objectContaining({
      operations: ["generation", "query.interpret", "query.rewrite"],
      request_limit: 100,
      estimated_token_limit: 500_000,
      operation_request_limits: {
        generation: 33,
        "query.interpret": 33,
        "query.rewrite": 33,
      },
    }),
  );
  expect(await screen.findByText(/已批准当前版本/)).toBeVisible();
});

it("远程图片识别待选图时只批准问答用途并给出后续入口", async () => {
  const user = userEvent.setup();
  const missing: CorpusAuthorizationStatus = {
    corpus_authorization_state: "MISSING",
    model_configuration_state: "CONFIGURED",
    model_authorization_state: "MISSING",
    budget_state: "MISSING",
    required_operations: ["generation", "query.interpret", "query.rewrite"],
    pending_operations: ["image.ocr"],
    fallback_reason_codes: ["CORPUS_AUTHORIZATION_MISSING"],
  };
  vi.spyOn(api, "modelSettings").mockResolvedValue({
    ...settings,
    generation_connection_id: "conn_test",
    generation_model: "qwen3.7-flash",
    rewrite_enabled: true,
    ocr_connection_id: "conn_test",
    ocr_model: "qwen3.5-ocr",
    ocr_enabled: true,
    corpus_authorization: missing,
  });
  const approved: CorpusAuthorizationStatus = {
    ...missing,
    corpus_authorization_state: "APPROVED",
    model_authorization_state: "APPROVED",
    budget_state: "AVAILABLE",
    fallback_reason_codes: [],
    manifest: {
      manifest_id: `cauth_${"a".repeat(32)}`,
      project_id: `prj_${"b".repeat(32)}`,
      knowledge_base_id: `kb_${"c".repeat(32)}`,
      active_index_revision_id: `irev_${"d".repeat(32)}`,
      active_document_digest: `sha256:${"e".repeat(64)}`,
      active_document_count: 2,
      provider_connection_id: "conn_test",
      provider_model: "qwen3.7-flash",
      operation_binding_identity: `sha256:${"f".repeat(64)}`,
      operations: ["generation", "query.interpret", "query.rewrite"],
      authorization_id: "synthetic-authorization",
      budget_campaign_id: "synthetic-budget",
      created_at: "2030-01-01T00:00:00+00:00",
      expires_at: "2030-02-01T00:00:00+00:00",
      policy_revision: "corpus-authorization-v1",
      approved_by_session_id: "sess_test",
    },
  };
  const approve = vi
    .spyOn(api, "approveCorpusAuthorization")
    .mockResolvedValue(approved);

  render(<KnowledgeBaseModels kbId="kb_test" />);
  expect(
    await screen.findByText(/图片识别已启用，但尚未选择具体图片/),
  ).toBeVisible();
  await user.click(screen.getByRole("button", { name: "批准当前活动资料" }));
  expect(
    screen.getByText(/本次批准不包含图片识别/),
  ).toBeVisible();
  expect(
    screen.getByText(/本次用途：回答生成、问题意图解释、问题改写/),
  ).toBeVisible();
  await user.click(screen.getByRole("button", { name: "确认批准当前版本" }));
  await waitFor(() => expect(approve).toHaveBeenCalledTimes(1));
  expect(approve.mock.calls[0][1].operations).toEqual([
    "generation",
    "query.interpret",
    "query.rewrite",
  ]);
});

it("只有远程图片识别且尚未选图时不展示无效批准按钮", async () => {
  vi.spyOn(api, "modelSettings").mockResolvedValue({
    ...settings,
    ocr_connection_id: "conn_test",
    ocr_model: "qwen3.5-ocr",
    ocr_enabled: true,
    corpus_authorization: {
      corpus_authorization_state: "NOT_REQUIRED",
      model_configuration_state: "NOT_CONFIGURED",
      model_authorization_state: "NOT_REQUIRED",
      budget_state: "NOT_REQUIRED",
      required_operations: [],
      pending_operations: ["image.ocr"],
      fallback_reason_codes: [],
    },
  });

  render(<KnowledgeBaseModels kbId="kb_test" />);
  expect(await screen.findByText(/图片识别待选择图片/)).toBeVisible();
  expect(screen.getByText(/请在下方文档行打开“图片识别”/)).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "批准当前活动资料" }),
  ).not.toBeInTheDocument();
});
