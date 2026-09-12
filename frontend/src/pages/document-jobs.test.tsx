import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import {
  ApiError,
  api,
  type Document,
  type Job,
  type RetrievalIngestionAuthorizationStatus,
} from "../api/client";
import { DocumentsPage } from "./DocumentsPage";
import { JobsPage } from "./JobsPage";
import { RevisionPage } from "./RevisionPage";

const consoleState = vi.hoisted(() => ({
  tokens: { admin: "session", query: "session" },
  scope: { projectId: "prj_test", kbId: "kb_test", revisionId: "" },
  setRevision: vi.fn(),
}));
vi.mock("../state/console-context", () => ({ useConsole: () => consoleState }));
const document: Document = {
  document_id: "doc_test",
  project_id: "prj_test",
  knowledge_base_id: "kb_test",
  display_name: "合成失败文档.docx",
  current_version_id: null,
  active_index_revision_id: null,
  status: "active",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};
const job: Job = {
  job_id: "job_test",
  project_id: "prj_test",
  knowledge_base_id: "kb_test",
  document_id: "doc_test",
  document_version_id: "dver_test",
  revision_id: "irev_test",
  state: "failed_retryable",
  stage: "chunking",
  attempt: 1,
  retryable: true,
  safe_error: "片段结构校验失败。",
  error_code: "INVALID_CHUNK",
  lease_owner: false,
  fencing_safe_status: "released",
  revision_available: false,
  required_action: null,
  slot_progress: [],
};
const scope = { offset: 0, page_size: 50, next_offset: null };

function ingestionAuthorizationStatus(
  overrides: Partial<RetrievalIngestionAuthorizationStatus> = {},
): RetrievalIngestionAuthorizationStatus {
  return {
    approval_allowed: true,
    authorization_state: "MISSING",
    budget_state: "MISSING",
    connection_budget_state: "READY",
    embedding_slot_count: 1,
    estimation_state: "UNAVAILABLE_PREBUILD",
    job_id: job.job_id,
    next_action: "approve",
    predecessor_index_revision_id: null,
    profile_revision_id: "pfr_test",
    reason_codes: ["RETRIEVAL_INGESTION_AUTHORIZATION_REQUIRED"],
    recommended_estimated_token_limit: 4096,
    recommended_operation_request_limits: {
      "embedding.document": 5,
      "embedding.query": 5,
      reranking: 5,
    },
    recommended_request_limit: 15,
    required_operations: ["embedding.document", "embedding.query", "reranking"],
    source_document_count: 1,
    source_size_bytes: 1024,
    target_index_revision_id: job.revision_id,
    ...overrides,
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
  consoleState.scope.revisionId = "";
  consoleState.setRevision.mockReset();
  vi.spyOn(api, "modelSettings").mockResolvedValue({
    generation_connection_id: null,
    generation_model: null,
    rewrite_enabled: false,
    ocr_connection_id: null,
    ocr_model: null,
    ocr_enabled: false,
    budget_campaign_id: null,
  });
  vi.spyOn(api, "listDocuments").mockResolvedValue({
    ...scope,
    items: [document],
  });
  vi.spyOn(api, "listJobs").mockResolvedValue({
    ...scope,
    total: 1,
    items: [job],
  });
});

it("上传回执只进入任务页，不把计划 Revision 当成可读版本", async () => {
  const user = userEvent.setup();
  const go = vi.fn();
  vi.spyOn(api, "uploadDocument").mockResolvedValue({
    ...job,
    state: "queued",
    stage: "queued",
    safe_error: null,
    error_code: null,
  });
  render(<DocumentsPage go={go} />);
  await screen.findByText("无当前版本，尚不可检索");

  await user.upload(
    screen.getByTestId("new-document-file"),
    new File(["synthetic"], "首次入库.docx"),
  );

  await waitFor(() => expect(go).toHaveBeenCalledWith("/jobs"));
  expect(consoleState.setRevision).not.toHaveBeenCalled();
});

it("无当前版本明确不可检索，204删除二次确认并刷新列表", async () => {
  const user = userEvent.setup();
  const remove = vi
    .spyOn(api, "deleteDocument")
    .mockResolvedValue({ statusCode: 204, document: undefined });
  render(<DocumentsPage go={vi.fn()} />);
  expect(await screen.findByText("无当前版本，尚不可检索")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "删除" }));
  expect(remove).not.toHaveBeenCalled();
  vi.mocked(api.listDocuments).mockResolvedValue({ ...scope, items: [] });
  await user.click(screen.getByRole("button", { name: "确认删除整个文档" }));
  expect(remove).toHaveBeenCalledWith(
    "session",
    "prj_test",
    "kb_test",
    "doc_test",
  );
  expect(await screen.findByText("暂无文档")).toBeVisible();
});

it("202仅显示已接收，失败文档重传使用同一逻辑文档版本端点", async () => {
  const user = userEvent.setup();
  vi.spyOn(api, "deleteDocument").mockResolvedValue({
    statusCode: 202,
    document: { status: "deleting" },
  });
  const upload = vi
    .spyOn(api, "uploadVersion")
    .mockRejectedValue(new Error("版本上传被拒绝"));
  const create = vi.spyOn(api, "uploadDocument");
  render(<DocumentsPage go={vi.fn()} />);
  await screen.findByText("无当前版本，尚不可检索");
  await user.click(screen.getByRole("button", { name: "删除" }));
  await user.click(screen.getByRole("button", { name: "确认删除整个文档" }));
  expect(await screen.findByText(/删除请求已接收/)).toBeVisible();
  await user.upload(
    screen.getByTestId("version-doc_test"),
    new File(["synthetic"], "合成.docx"),
  );
  expect(upload).toHaveBeenCalledWith(
    "session",
    "prj_test",
    "kb_test",
    "doc_test",
    expect.any(File),
    expect.any(String),
  );
  expect(create).not.toHaveBeenCalled();
  expect(await screen.findByText("版本上传被拒绝")).toBeVisible();
});

it("任务展示真实失败阶段和事件，released不作为失败原因，重试同一job", async () => {
  const user = userEvent.setup();
  vi.spyOn(api, "getJob").mockResolvedValue(job);
  vi.spyOn(api, "jobTrace").mockResolvedValue({
    trace_id: "trace_test",
    job,
    events: [
      {
        event_name: "chunking.failed",
        occurred_at: "2026-01-01T00:00:00Z",
        attributes: [["source_line", 42]],
      },
    ],
  });
  const retry = vi
    .spyOn(api, "retryJob")
    .mockResolvedValue({ ...job, state: "queued" });
  render(<JobsPage go={vi.fn()} />);
  await user.click(
    await screen.findByRole("button", { name: "查看原因/日志" }),
  );
  const dialog = screen.getByRole("dialog", { name: "任务原因与安全日志" });
  expect(within(dialog).getByText("INVALID_CHUNK")).toBeVisible();
  expect(await within(dialog).findByText(/chunking.failed/)).toBeVisible();
  await user.click(within(dialog).getByRole("button", { name: "关闭" }));
  await user.click(screen.getByRole("button", { name: "重试此任务" }));
  await waitFor(() =>
    expect(retry).toHaveBeenCalledWith("session", "job_test"),
  );
});

it("授权阻塞任务不开放幻影版本，管理员确认后批准并重试同一job", async () => {
  const user = userEvent.setup();
  const authorizationJob: Job = {
    ...job,
    error_code: "RETRIEVAL_INGESTION_AUTHORIZATION_REQUIRED",
    safe_error: "此文档版本尚未获准发送给远程检索服务。",
    required_action: "approve_retrieval",
  };
  vi.mocked(api.listJobs).mockResolvedValue({
    ...scope,
    total: 1,
    items: [authorizationJob],
  });
  vi.spyOn(api, "retrievalIngestionAuthorization").mockResolvedValue({
    approval_allowed: true,
    authorization_state: "MISSING",
    budget_state: "MISSING",
    connection_budget_state: "READY",
    estimation_state: "UNAVAILABLE_PREBUILD",
    job_id: job.job_id,
    profile_revision_id: "pfr_test",
    predecessor_index_revision_id: null,
    target_index_revision_id: job.revision_id,
    source_document_count: 1,
    source_size_bytes: 1024,
    embedding_slot_count: 1,
    required_operations: ["embedding.document", "embedding.query", "reranking"],
    recommended_request_limit: 15,
    recommended_estimated_token_limit: 4096,
    recommended_operation_request_limits: {
      "embedding.document": 5,
      "embedding.query": 5,
      reranking: 5,
    },
    next_action: "approve",
    reason_codes: ["RETRIEVAL_INGESTION_AUTHORIZATION_REQUIRED"],
  });
  const approve = vi
    .spyOn(api, "approveRetrievalIngestionAuthorization")
    .mockResolvedValue({
      approval_allowed: false,
      authorization_state: "APPROVED",
      budget_state: "AVAILABLE",
      connection_budget_state: "READY",
      estimation_state: "UNAVAILABLE_PREBUILD",
      job_id: job.job_id,
      profile_revision_id: "pfr_test",
      predecessor_index_revision_id: null,
      target_index_revision_id: job.revision_id,
      source_document_count: 1,
      source_size_bytes: 1024,
      embedding_slot_count: 1,
      required_operations: [
        "embedding.document",
        "embedding.query",
        "reranking",
      ],
      recommended_request_limit: 15,
      recommended_estimated_token_limit: 4096,
      recommended_operation_request_limits: {
        "embedding.document": 5,
        "embedding.query": 5,
        reranking: 5,
      },
      next_action: "continue",
      reason_codes: [],
    });
  const retry = vi.spyOn(api, "retryJob").mockResolvedValue({
    ...authorizationJob,
    state: "queued",
    required_action: null,
  });
  render(<JobsPage go={vi.fn()} />);

  expect(await screen.findByText("此任务尚无可读索引版本。")).toBeVisible();
  expect(screen.queryByRole("button", { name: "检查版本" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "处理检索授权" }));
  expect(await screen.findByText(/尚未获得继续发送/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "批准并继续同一任务" }));

  await waitFor(() => expect(approve).toHaveBeenCalledTimes(1));
  expect(retry).toHaveBeenCalledWith("session", "job_test");
  expect(consoleState.setRevision).not.toHaveBeenCalled();
});

it("服务端未确认授权可用时不自动重试，并在弹窗保留错误", async () => {
  const user = userEvent.setup();
  const authorizationJob: Job = {
    ...job,
    error_code: "RETRIEVAL_INGESTION_AUTHORIZATION_REQUIRED",
    safe_error: "此文档版本正在等待真实检索授权。",
    required_action: "approve_retrieval",
  };
  vi.mocked(api.listJobs).mockResolvedValue({
    ...scope,
    total: 1,
    items: [authorizationJob],
  });
  vi.spyOn(api, "retrievalIngestionAuthorization").mockResolvedValue(
    ingestionAuthorizationStatus(),
  );
  vi.spyOn(api, "approveRetrievalIngestionAuthorization").mockResolvedValue(
    ingestionAuthorizationStatus({
      authorization_state: "STALE_JOB",
      budget_state: "BLOCKED",
      next_action: "reauthorize",
      reason_codes: ["RETRIEVAL_INGESTION_JOB_CHANGED"],
    }),
  );
  const retry = vi.spyOn(api, "retryJob");
  render(<JobsPage go={vi.fn()} />);

  await user.click(await screen.findByRole("button", { name: "处理检索授权" }));
  await user.click(screen.getByRole("button", { name: "批准并继续同一任务" }));

  expect(await screen.findByText(/服务端尚未确认授权与预算可用/)).toBeVisible();
  expect(screen.getByText(/此前是否发生过发送/)).toBeVisible();
  expect(retry).not.toHaveBeenCalled();
});

it("冻结方案已被替代时引导回文档管理，不展示不可达的批准动作", async () => {
  const user = userEvent.setup();
  const go = vi.fn();
  const authorizationJob: Job = {
    ...job,
    error_code: "RETRIEVAL_INGESTION_PROFILE_CHANGED",
    safe_error: "任务冻结的检索方案已变化。",
    required_action: "approve_retrieval",
  };
  vi.mocked(api.listJobs).mockResolvedValue({
    ...scope,
    total: 1,
    items: [authorizationJob],
  });
  vi.spyOn(api, "retrievalIngestionAuthorization").mockResolvedValue(
    ingestionAuthorizationStatus({
      approval_allowed: false,
      authorization_state: "STALE_PROFILE",
      budget_state: "BLOCKED",
      connection_budget_state: "BLOCKED",
      next_action: "review_document",
      reason_codes: ["RETRIEVAL_INGESTION_PROFILE_CHANGED"],
    }),
  );
  const approve = vi.spyOn(api, "approveRetrievalIngestionAuthorization");
  render(<JobsPage go={go} />);

  await user.click(await screen.findByRole("button", { name: "处理检索授权" }));
  expect(
    await screen.findByText(/任务冻结的检索方案已被另一方案替代/),
  ).toBeVisible();
  expect(screen.queryByRole("button", { name: /批准并继续/ })).toBeNull();
  await user.click(screen.getByRole("button", { name: "返回文档管理" }));

  expect(go).toHaveBeenCalledWith("/documents");
  expect(approve).not.toHaveBeenCalled();
});

it("旧会话中的计划 Revision 先检查存在性，404 时不再并发读取子资源", async () => {
  const user = userEvent.setup();
  const go = vi.fn();
  consoleState.scope.revisionId = "irev_missing";
  vi.spyOn(api, "inspectRevision").mockRejectedValue(new ApiError(404, {}));
  const listChunks = vi.spyOn(api, "listChunks");
  const reports = vi.spyOn(api, "revisionReports");

  render(<RevisionPage go={go} />);

  expect(
    await screen.findByRole("heading", { name: "索引版本尚不可用" }),
  ).toBeVisible();
  expect(consoleState.setRevision).toHaveBeenCalledWith("");
  expect(listChunks).not.toHaveBeenCalled();
  expect(reports).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "返回任务列表" }));
  expect(go).toHaveBeenCalledWith("/jobs");
});

it("可读 Revision 通过检查后展示内容与结构依赖", async () => {
  const user = userEvent.setup();
  consoleState.scope.revisionId = "irev_readable";
  vi.spyOn(api, "inspectRevision").mockResolvedValue({
    activation_history: [],
    active: false,
    actual_chunk_count: 0,
    actual_document_count: 1,
    chunk_payload_schema: "chunk-v1",
    created_at: "2026-01-01T00:00:00Z",
    expected_chunk_count: 0,
    expected_document_count: 1,
    fts_count: 0,
    index_fingerprint: "sha256:test",
    knowledge_base_id: "kb_test",
    lexical_schema: [],
    project_id: "prj_test",
    revision_id: "irev_readable",
    serving_compatibility_version: "v1",
    serving_fingerprint: "sha256:test",
    slot_coverages: [],
    state: "retired",
    vector_schema: [],
    writer_status: "committed",
  });
  const listChunks = vi.spyOn(api, "listChunks").mockResolvedValue({
    items: [
      {
        child_group_ids: [],
        chunk_id: "chunk_test",
        chunker_fingerprint: "sha256:test",
        citation_text: "负责公开合成质量目标。",
        content_sha256: "sha256:test",
        context_dependencies: [
          {
            origin: "inferred_numbered_heading",
            relationship_type: "heading_context",
            source_node_id: "node_heading",
          },
        ],
        embedding_text:
          "位置：4 部门职责 / 4.1 合成经理\n负责公开合成质量目标。",
        heading_path: ["4 部门职责", "4.1 合成经理"],
        identifiers: [],
        index_revision_id: "irev_readable",
        knowledge_base_id: "kb_test",
        lexical_text: "4 部门职责 4.1 合成经理 负责公开合成质量目标",
        metadata: [],
        neighbor_group_id: "group_test",
        note_refs: [],
        project_id: "prj_test",
        role: "text",
        schema_version: "3",
        section_id: "section_test",
        source_spans: [],
        token_count: 20,
        token_count_is_estimate: false,
        tokenizer_id: "test",
        version: {
          content_sha256: "sha256:document",
          document_id: "doc_test",
          document_version_id: "dver_test",
        },
      },
    ],
    total: 1,
    offset: 0,
    page_size: 50,
    next_offset: null,
  });
  const reports = vi
    .spyOn(api, "revisionReports")
    .mockResolvedValue({ items: [] });

  render(<RevisionPage go={vi.fn()} />);

  expect(
    await screen.findByRole("heading", { name: "索引版本" }),
  ).toBeVisible();
  await user.click(screen.getByText("text · section_test · chunk_test"));
  expect(screen.getByText("4 部门职责 / 4.1 合成经理")).toBeVisible();
  expect(
    screen.getByText("inferred_numbered_heading", { exact: false }),
  ).toBeVisible();
  expect(screen.getByText("node_heading", { exact: false })).toBeVisible();
  expect(listChunks).toHaveBeenCalledWith(
    "session",
    "prj_test",
    "kb_test",
    "irev_readable",
  );
  expect(reports).toHaveBeenCalledWith(
    "session",
    "prj_test",
    "kb_test",
    "irev_readable",
  );
});
