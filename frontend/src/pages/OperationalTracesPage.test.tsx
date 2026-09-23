import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import {
  api,
  type OperationalTraceDetail,
  type OperationalTraceRoot,
} from "../api/client";
import { OperationalTracesPage } from "./OperationalTracesPage";

vi.mock("../state/console-context", () => ({
  useConsole: () => ({
    setRevision: vi.fn(),
    scope: {
      projectId: "prj_test",
      kbId: "kb_test",
      revisionId: "irev_test",
    },
  }),
}));

const root: OperationalTraceRoot = {
  trace_id: "trace_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  schema_version: "2",
  mode: "DIAGNOSTIC",
  kind: "query",
  status: "ANSWERED",
  created_at: "2026-09-08T10:00:00Z",
  finished_at: "2026-09-08T10:00:00.024Z",
  duration_ms: 24,
  project_id: "prj_test",
  knowledge_base_id: "kb_test",
  request_id: "trace_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  job_id: null,
  document_id: null,
  revision_id: "irev_test",
  profile_id: "profile-test",
  index_fingerprint: "sha256:index",
  serving_fingerprint: "sha256:serving",
  source_revision: "revision-test",
  capture_complete: true,
  capture_incomplete_reason: null,
  dropped_span_count: 0,
  dropped_decision_count: 0,
  writer_queue_high_water: 3,
};

const detail: OperationalTraceDetail = {
  trace: root,
  spans: [
    {
      trace_id: root.trace_id,
      span_id: "1111111111111111",
      parent_span_id: null,
      sequence: 1,
      name: "rag.query",
      kind: "CHAIN",
      started_at: root.created_at,
      finished_at: root.finished_at,
      duration_ms: 24,
      status: "OK",
      reason_code: "ANSWERED",
      attributes: {},
    },
    {
      trace_id: root.trace_id,
      span_id: "2222222222222222",
      parent_span_id: "1111111111111111",
      sequence: 2,
      name: "provider.generation",
      kind: "LLM",
      started_at: "2026-09-08T10:00:00.004Z",
      finished_at: "2026-09-08T10:00:00.010Z",
      duration_ms: 6,
      status: "OK",
      reason_code: "PROVIDER_CALLED",
      attributes: { operation: "generation", call_count: 1 },
    },
  ],
  candidate_decisions: [
    {
      trace_id: root.trace_id,
      sequence: 1,
      stage: "fusion",
      chunk_id: "chunk_test",
      candidate_id: "chunk_test",
      selected: true,
      reason_code: "FUSION_SELECTED",
      details: {},
      rank: 1,
      score_type: "rrf",
      score: 0.5,
    },
  ],
  artifacts: [
    {
      trace_id: root.trace_id,
      artifact_id: "artifact_test",
      kind: "retrieval_diagnostics",
      media_type: "application/json",
      sha256: "a".repeat(64),
      original_bytes: 42,
      compressed_bytes: 30,
      created_at: root.created_at,
    },
  ],
  legacy_flat_events: [],
};

beforeEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState({}, "", "/operational-traces");
  vi.spyOn(api, "listOperationalTraces").mockResolvedValue({
    items: [root],
    page: 1,
    page_size: 30,
    total: 1,
  });
  vi.spyOn(api, "operationalTraceDetail").mockResolvedValue(detail);
});

it("湾事通技术 Trace 列表和详情显示 History 关联的提问者", async () => {
  const user = userEvent.setup();
  const identified: OperationalTraceRoot = {
    ...root,
    requester: {
      identity_source: "RDMS_SSO",
      external_user_id: "1001",
      display_name_at_request: null,
      label: "RDMS用户 #1001",
      name_state: "NOT_CAPTURED",
    },
  };
  render(
    <OperationalTracesPage
      fixedScope
      services={{
        list: vi.fn().mockResolvedValue({
          items: [identified],
          page: 1,
          page_size: 30,
          total: 1,
        }),
        detail: vi.fn().mockResolvedValue({ ...detail, trace: identified }),
      }}
    />,
  );
  expect(await screen.findByRole("cell", { name: "RDMS用户 #1001" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "查看技术详情" }));
  await waitFor(() =>
    expect(screen.getAllByText("RDMS用户 #1001").length).toBeGreaterThan(1),
  );
});

it("列表可筛选，详情提供 waterfall 与候选漏斗", async () => {
  const user = userEvent.setup();
  render(<OperationalTracesPage />);
  expect((await screen.findAllByText(root.trace_id)).length).toBeGreaterThan(0);
  await user.selectOptions(screen.getByLabelText("类型"), "query");
  await user.selectOptions(screen.getByLabelText("捕获模式"), "DIAGNOSTIC");
  await user.click(screen.getByRole("button", { name: "筛选 Trace" }));
  await waitFor(() =>
    expect(api.listOperationalTraces).toHaveBeenLastCalledWith(
      expect.objectContaining({
        kind: "query",
        capture_mode: "DIAGNOSTIC",
        project_id: "prj_test",
        knowledge_base_id: "kb_test",
      }),
      expect.any(AbortSignal),
    ),
  );
  await user.click(screen.getByRole("button", { name: "查看技术详情" }));
  const panel = await screen.findByRole("region", {
    name: "Operational Trace 详情",
  });
  expect(within(panel).getByLabelText("Span waterfall")).toBeVisible();
  expect(within(panel).getByText(/未采集。这通常表示旧 Trace/)).toBeVisible();
  await user.click(within(panel).getByRole("tab", { name: "候选漏斗" }));
  expect(within(panel).getByText("chunk_test")).toBeVisible();
  expect(within(panel).getByText("rrf: 0.5")).toBeVisible();
});

it("查询详情展示部门影子建议、实际范围与最终引用部门", async () => {
  const user = userEvent.setup();
  vi.mocked(api.operationalTraceDetail).mockResolvedValue({
    ...detail,
    spans: [
      ...detail.spans,
      {
        trace_id: root.trace_id,
        span_id: "3333333333333333",
        parent_span_id: "1111111111111111",
        sequence: 3,
        name: "retrieval.department_route_shadow",
        kind: "CHAIN",
        started_at: root.created_at,
        finished_at: root.finished_at,
        duration_ms: 2,
        status: "OK",
        reason_code: "STARTED",
        attributes: {
          route_revision: "wanshitong-department-shadow-v1",
          profile_revision: "sha256:" + "4".repeat(64),
          actual_scope_kind: "SOURCE_RESOLVED",
          top1_department_key: "research",
          top1_score_bucket: "EXPLICIT",
          top2_department_key: "finance",
          top2_score_bucket: "0_30_TO_0_49",
          confidence: "HIGH",
          recommended_scope: "TOP1",
          final_cited_department_keys: ["research"],
          department_filter_applied: false,
          embedding_reused: true,
          extra_provider_calls: 0,
          status: "COMPUTED",
          reason_codes: ["EXPLICIT_DEPARTMENT_UNIQUE"],
        },
      },
    ],
  });

  render(<OperationalTracesPage />);
  await user.click(await screen.findByRole("button", { name: "查看技术详情" }));
  const tracePanel = await screen.findByRole("region", {
    name: "Operational Trace 详情",
  });
  const shadowPanel = within(tracePanel).getByRole("region", {
    name: "部门影子路由",
  });

  expect(
    within(shadowPanel).getByText("优先 Top 1 部门 · 高置信"),
  ).toBeVisible();
  expect(
    within(shadowPanel).getByText("research · 显式唯一部门"),
  ).toBeVisible();
  expect(within(shadowPanel).getByText("finance · 中分段")).toBeVisible();
  expect(within(shadowPanel).getByText("显式文档范围（已解析）")).toBeVisible();
  expect(
    within(shadowPanel).getByText(
      "实际检索未使用部门过滤；建议只用于管理员事后评估。",
    ),
  ).toBeVisible();
  expect(within(shadowPanel).getByText("research")).toBeVisible();
  expect(within(shadowPanel).queryByText(/0\.\d+/)).toBeNull();
});

it("Artifact 只在管理员点击后读取并按文本渲染", async () => {
  const user = userEvent.setup();
  const read = vi.spyOn(api, "operationalTraceArtifact").mockResolvedValue({
    mediaType: "application/json",
    sha256: "a".repeat(64),
    body: JSON.stringify({ safe: "<img src=x onerror=alert(1)>" }),
  });
  render(<OperationalTracesPage />);
  await user.click(await screen.findByRole("button", { name: "查看技术详情" }));
  const panel = await screen.findByRole("region", {
    name: "Operational Trace 详情",
  });
  expect(read).not.toHaveBeenCalled();
  await user.click(within(panel).getByRole("tab", { name: "Artifact" }));
  await user.click(within(panel).getByRole("button", { name: "按需读取" }));
  await waitFor(() =>
    expect(read).toHaveBeenCalledWith(root.trace_id, "artifact_test"),
  );
  expect(within(panel).getByText(/<img src=x onerror=alert/)).toBeVisible();
  expect(panel.querySelector("img")).toBeNull();
});

it("Job 深链只传稳定 job_id，不要求先知道 Trace ID", async () => {
  window.history.replaceState(
    {},
    "",
    "/operational-traces?job_id=job_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  );
  render(<OperationalTracesPage />);
  await waitFor(() =>
    expect(api.listOperationalTraces).toHaveBeenCalledWith(
      expect.objectContaining({
        job_id: "job_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      }),
      expect.any(AbortSignal),
    ),
  );
});

it("旧 32hex 深链可直接读取详情并精确筛选", async () => {
  const legacyId = "a".repeat(32);
  window.history.replaceState(
    {},
    "",
    `/operational-traces?trace_id=${legacyId}`,
  );
  render(<OperationalTracesPage />);

  await waitFor(() =>
    expect(api.listOperationalTraces).toHaveBeenCalledWith(
      expect.objectContaining({ trace_id: legacyId }),
      expect.any(AbortSignal),
    ),
  );
  await waitFor(() =>
    expect(api.operationalTraceDetail).toHaveBeenCalledWith(
      legacyId,
      expect.any(AbortSignal),
    ),
  );
  expect(
    await screen.findByRole("region", { name: "Operational Trace 详情" }),
  ).toBeVisible();
});

it("反馈筛选和选择计数进入 API，批量导出失败后解除 busy", async () => {
  const user = userEvent.setup();
  let rejectExport!: (error: Error) => void;
  vi.spyOn(api, "exportOperationalTraces").mockImplementation(
    () =>
      new Promise((_resolve, reject) => {
        rejectExport = reject;
      }),
  );
  render(<OperationalTracesPage />);
  await screen.findAllByText(root.trace_id);
  await user.selectOptions(screen.getByLabelText("用户反馈"), "not-useful");
  await user.click(screen.getByRole("button", { name: "筛选 Trace" }));
  await waitFor(() =>
    expect(api.listOperationalTraces).toHaveBeenLastCalledWith(
      expect.objectContaining({ feedback_useful: false }),
      expect.any(AbortSignal),
    ),
  );
  await user.click(screen.getByRole("button", { name: "选择本页" }));
  expect(screen.getByText("已选择 1 条")).toBeVisible();
  await user.click(
    screen.getByRole("button", { name: "导出已选技术 Trace" }),
  );
  expect(screen.getByRole("button", { name: "正在导出…" })).toBeDisabled();
  rejectExport(new Error("合成批量导出失败"));
  expect(await screen.findByText("合成批量导出失败")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "导出已选技术 Trace" }),
  ).toBeEnabled();
});

it("到期 prune 需确认，失败后解除 busy 且可重试", async () => {
  const user = userEvent.setup();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  let rejectPrune!: (error: Error) => void;
  vi.spyOn(api, "pruneOperationalTraces").mockImplementation(
    () =>
      new Promise((_resolve, reject) => {
        rejectPrune = reject;
      }),
  );
  render(<OperationalTracesPage />);
  await screen.findAllByText(root.trace_id);
  await user.click(screen.getByRole("button", { name: "清理已到期 Trace" }));
  expect(screen.getByRole("button", { name: "清理中…" })).toBeDisabled();
  rejectPrune(new Error("合成 prune 失败"));
  expect(await screen.findByText("合成 prune 失败")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "清理已到期 Trace" }),
  ).toBeEnabled();
});
