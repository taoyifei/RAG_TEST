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
  await user.click(within(panel).getByRole("tab", { name: "候选漏斗" }));
  expect(within(panel).getByText("chunk_test")).toBeVisible();
  expect(within(panel).getByText("rrf: 0.5")).toBeVisible();
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
