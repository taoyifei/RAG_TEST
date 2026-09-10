import { StrictMode } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { api, type Evidence, type HistoryEntry } from "../api/client";
import { HistoryPage } from "./HistoryPage";

vi.mock("../state/console-context", () => ({
  useConsole: () => ({
    tokens: { admin: "session", query: "session" },
    scope: { projectId: "prj_test", kbId: "kb_test", revisionId: "" },
  }),
}));
const evidence: Evidence = {
  metadata: [],
  evidence_id: "support_test",
  chunk_id: "chunk_test",
  citation_text: "合成资料原文",
  source_label: "合成手册",
  source_spans: [],
  retrieval_origins: ["lexical"],
  selection_reason: "supported",
  publishable: true,
  table_context: false,
  heading_path: [],
  quality_flags: [],
  document_id: "doc_test",
  document_version_id: "dver_test",
};
const item: HistoryEntry = {
  trace_id: "trace_test",
  project_id: "prj_test",
  knowledge_base_id: "kb_test",
  created_at: "2026-01-01T00:00:00Z",
  status: "ANSWERED",
  duration_ms: 18,
  question: "合成岗位职责是什么",
  answer: "已确认的合成职责",
  answer_summary: "已确认的合成职责",
  body_saved: true,
  body_available: true,
  body_message: "本地加密保存",
  cache_hit: true,
  models: [],
  active_index_revision_id: "irev_test",
  generation_mode: "extractive",
  provider_usage: [
    {
      operation: "generation",
      call_count: 0,
      usage: null,
      reason_code: "CACHE_HIT",
    },
  ],
  events: [
    {
      event_name: "cache",
      occurred_at: "2026-01-01T00:00:00Z",
      attributes: [["result", "hit"]],
    },
  ],
  result: { evidence: [evidence] },
};
function page(items = [item], total = items.length, offset = 0) {
  return {
    items,
    total,
    offset,
    page_size: 20,
    body_enabled: true,
    retention_days: 7,
    storage: "local_encrypted_sqlite",
  };
}
beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "listKnowledgeBases").mockResolvedValue({
    items: [],
    offset: 0,
    page_size: 50,
  });
  vi.spyOn(api, "listHistory").mockResolvedValue(page());
  vi.spyOn(api, "historyDetail").mockResolvedValue(item);
  vi.spyOn(api, "getFeedback").mockResolvedValue({ feedback: null });
});

it("历史可筛选并分页，不要求预先输入 trace_id", async () => {
  const user = userEvent.setup();
  vi.mocked(api.listHistory).mockResolvedValue(page([item], 21));
  render(<HistoryPage />);
  expect(await screen.findByText("合成岗位职责是什么")).toBeVisible();
  await user.selectOptions(screen.getByLabelText("结果"), "FAILED");
  await user.type(screen.getByLabelText("问题关键词"), "岗位");
  await user.click(screen.getByRole("button", { name: "筛选历史" }));
  await waitFor(() =>
    expect(api.listHistory).toHaveBeenLastCalledWith(
      expect.objectContaining({
        project_id: "prj_test",
        knowledge_base_id: "kb_test",
        keyword: "岗位",
        status: "FAILED",
        offset: 0,
      }),
      expect.any(AbortSignal),
    ),
  );
  await user.click(await screen.findByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(api.listHistory).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: 20 }),
      expect.any(AbortSignal),
    ),
  );
});

it("详情展示调用0次的原因，原文重新鉴权失败后隐藏历史正文", async () => {
  const user = userEvent.setup();
  vi.spyOn(api, "readEvidenceSource").mockRejectedValue(
    new Error("来源已删除"),
  );
  const hidden = {
    ...item,
    body_available: false,
    body_message: "来源已删除或范围已失效，正文不可查看。",
    question: null,
    answer: null,
    result: null,
  };
  vi.mocked(api.historyDetail)
    .mockResolvedValueOnce(item)
    .mockResolvedValue(hidden);
  render(<HistoryPage />);
  await user.click(
    await screen.findByRole("button", { name: "查看详情与过程" }),
  );
  const dialog = await screen.findByRole("dialog", { name: "问答与检索过程" });
  expect(await within(dialog).findByText("0 次")).toBeVisible();
  expect(within(dialog).getByText("CACHE_HIT")).toBeVisible();
  await user.click(
    within(dialog).getByRole("button", { name: "核对当前原文" }),
  );
  expect(await within(dialog).findByText(hidden.body_message)).toBeVisible();
  expect(within(dialog).queryByText("已确认的合成职责")).toBeNull();
  expect(api.readEvidenceSource).toHaveBeenCalledWith(
    "session",
    "prj_test",
    "kb_test",
    "irev_test",
    evidence,
    expect.any(AbortSignal),
  );
});

it("StrictMode下仍能加载详情，未保存正文不会假装恢复", async () => {
  const user = userEvent.setup();
  vi.mocked(api.historyDetail).mockResolvedValue({
    ...item,
    body_saved: false,
    body_available: false,
    body_message: "本次未保存正文",
    question: null,
    answer: null,
    result: null,
  });
  render(
    <StrictMode>
      <HistoryPage />
    </StrictMode>,
  );
  await user.click(
    await screen.findByRole("button", { name: "查看详情与过程" }),
  );
  const dialog = await screen.findByRole("dialog", { name: "问答与检索过程" });
  expect(await within(dialog).findByText("本次未保存正文")).toBeVisible();
  expect(within(dialog).queryByRole("heading", { name: "问题" })).toBeNull();
});

it("清理历史必须二次确认并明确影响全部知识库", async () => {
  const user = userEvent.setup();
  const clear = vi.spyOn(api, "clearHistory").mockResolvedValue(undefined);
  render(<HistoryPage />);
  await user.click(screen.getByRole("button", { name: "清理全部历史" }));
  expect(clear).not.toHaveBeenCalled();
  expect(screen.getByText(/本机所有项目和知识库/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "确认清理全部历史" }));
  expect(clear).toHaveBeenCalledTimes(1);
});

it("选择本页后批量下载确定性排序的支持包并可清空选择", async () => {
  const user = userEvent.setup();
  const second = { ...item, trace_id: "trace_alpha", question: "第二条" };
  vi.mocked(api.listHistory).mockResolvedValue(page([item, second]));
  const exportBatch = vi.spyOn(api, "exportHistoryTraces").mockResolvedValue({
    blob: new Blob(["synthetic"], { type: "application/zip" }),
    filename: "server-safe.zip",
  });
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:synthetic");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
    () => undefined,
  );

  render(<HistoryPage />);
  await screen.findByText("合成岗位职责是什么");
  await user.click(screen.getByRole("button", { name: "选择本页" }));
  expect(screen.getByText("已选择 2 条")).toBeVisible();
  await user.click(
    screen.getByRole("button", { name: "下载已选支持包" }),
  );
  await user.click(screen.getByRole("button", { name: "确认下载支持包" }));
  expect(exportBatch).toHaveBeenCalledWith(
    ["trace_alpha", "trace_test"],
    false,
  );
  await user.click(screen.getByRole("button", { name: "清空选择" }));
  expect(screen.getByText("已选择 0 条")).toBeVisible();
});
