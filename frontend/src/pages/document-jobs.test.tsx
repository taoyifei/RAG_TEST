import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { api, type Document, type Job } from "../api/client";
import { DocumentsPage } from "./DocumentsPage";
import { JobsPage } from "./JobsPage";

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
  slot_progress: [],
};
const scope = { offset: 0, page_size: 50, next_offset: null };
beforeEach(() => {
  vi.restoreAllMocks();
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
