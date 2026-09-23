import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/client";
import { AdminFeedbackPage } from "./AdminFeedbackPage";
import {
  wanshitongAdminApi,
  type FeedbackDetail,
  type FeedbackListItem,
} from "./adminApi";

const traceId = `trace_${"d".repeat(32)}`;
const documentId = `doc_${"1".repeat(32)}`;
const versionId = `dver_${"2".repeat(32)}`;

const item: FeedbackListItem = {
  trace_id: traceId,
  created_at: "2026-09-22T02:00:00+00:00",
  updated_at: "2026-09-22T02:01:00+00:00",
  question_summary: "材料核验应该引用哪份指南？",
  question_sha256: "a".repeat(64),
  final_status: "ANSWERED",
  useful: false,
  reason_code: "WRONG_SOURCE",
  reason_detail: "WRONG_SOURCE",
  comment_present: true,
  feedback_revision: 2,
  answer_path: "llm",
  duration_ms: 812,
  review_status: "REVIEWED",
  root_cause: "WRONG_SOURCE",
  review_version: 1,
  new_feedback_pending: true,
};

const detail: FeedbackDetail = {
  trace_id: traceId,
  feedback: {
    useful: false,
    reason_code: "WRONG_SOURCE",
    reason_detail: "WRONG_SOURCE",
    comment: "引用版本不对。",
    comment_available: true,
    comment_unavailable_reason: null,
    feedback_revision: 2,
    projection_state: "APPLIED",
    created_at: item.created_at,
    updated_at: item.updated_at,
  },
  review: {
    review_status: "REVIEWED",
    root_cause: "WRONG_SOURCE",
    note: "初次复核说明",
    note_available: true,
    note_unavailable_reason: null,
    selected_source_document_id: null,
    selected_source_version_id: null,
    reviewed_feedback_revision: 1,
    evaluation_candidate: false,
    fix_reference: null,
    verification_references: [],
    review_version: 1,
    reviewed_by_admin: "admin_session",
    updated_at: "2026-09-22T02:02:00+00:00",
    new_feedback_pending: true,
  },
  history: {
    available: true,
    unavailable_reason: null,
    question: "材料核验应该引用哪份指南？",
    answer: "应引用办事指南。",
    final_status: "ANSWERED",
    duration_ms: 812,
  },
  citations: [
    {
      document_id: documentId,
      document_version_id: versionId,
      display_name: "办事指南.docx",
      source_label: "材料核验",
      selected_source_eligible: true,
    },
  ],
  trace_projection: {
    question_understanding: {
      context_mode: null,
      resolved_root_hash: null,
      planner_called: null,
      atom_count: null,
    },
    retrieval_evidence: {
      root_candidate_count: 3,
      atom_candidate_count: null,
      generation_evidence_count: 1,
      exclusion_reasons: [],
      source_group_status: "complete",
    },
    generation: {
      answer_path: "llm",
      generation_called: true,
      generated_claim_count: 1,
      fallback: false,
    },
    validation_coverage: {
      accepted_claim_count: 1,
      published_claim_count: 1,
      rejection_distribution: {},
      atom_coverage: null,
      terminal_reason: "ANSWER_SUPPORTED",
    },
  },
  operational_trace: null,
  operational_trace_unavailable_reason: "TRACE_NOT_FOUND",
};

afterEach(() => vi.restoreAllMocks());

describe("反馈与优化待办页", () => {
  it("展示四段详情并携带版本和受权来源保存复核", async () => {
    vi.spyOn(wanshitongAdminApi, "questionAnalytics").mockResolvedValue({
      run: null,
      latest_run: null,
      items: [],
      total: 0,
      next_offset: null,
    });
    vi.spyOn(wanshitongAdminApi, "listFeedback").mockResolvedValue({
      items: [item],
      total: 1,
      page_size: 20,
      offset: 0,
      next_offset: null,
    });
    vi.spyOn(wanshitongAdminApi, "feedbackStatistics").mockResolvedValue({
      evaluated_count: 1,
      helpful_count: 0,
      helpful_rate: 0,
      pending_count: 1,
      confirmed_wrong_source_count: 1,
      confirmed_false_refusal_count: 0,
      latency_ms: {
        actual_sso_users: { count: 1, p50: 812, p95: 812 },
        non_sso_or_replay: { count: 0, p50: null, p95: null },
      },
    });
    const detailRead = vi
      .spyOn(wanshitongAdminApi, "feedbackDetail")
      .mockResolvedValue(detail);
    const review = vi
      .spyOn(wanshitongAdminApi, "reviewFeedback")
      .mockResolvedValue({
        ...detail,
        review: {
          ...detail.review,
          review_version: 2,
          reviewed_feedback_revision: 2,
          new_feedback_pending: false,
        },
      });
    const user = userEvent.setup();
    render(<AdminFeedbackPage />);

    expect(await screen.findByText("材料核验应该引用哪份指南？")).toBeVisible();
    expect(screen.getByText("有新反馈待复查")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "查看并复核" }));

    expect(
      await screen.findByRole("dialog", { name: "反馈与优化待办详情" }),
    ).toBeVisible();
    for (const heading of ["问题理解", "检索 / 证据", "生成", "校验 / 覆盖"]) {
      expect(screen.getByRole("heading", { name: heading })).toBeVisible();
    }
    expect(screen.getAllByText("未采集").length).toBeGreaterThan(0);
    expect(screen.getAllByText("办事指南.docx").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("link", { name: "查看受权文档记录" }),
    ).toHaveAttribute(
      "href",
      `/api/v1/admin/wanshitong/documents/${documentId}`,
    );

    await user.selectOptions(screen.getByLabelText("处理状态"), "REVIEWED");
    await user.selectOptions(screen.getByLabelText("根因"), "WRONG_SOURCE");
    const note = screen.getByLabelText("管理员 Note");
    await user.clear(note);
    await user.type(note, "重新核对后确认错来源。");
    await user.selectOptions(
      screen.getByLabelText("更合适的已授权引用"),
      `${documentId}::${versionId}`,
    );
    await user.click(screen.getByRole("button", { name: "保存复核" }));

    await waitFor(() => expect(review).toHaveBeenCalledOnce());
    expect(review).toHaveBeenCalledWith(
      traceId,
      expect.objectContaining({
        expected_version: 1,
        review_status: "REVIEWED",
        root_cause: "WRONG_SOURCE",
        note: "重新核对后确认错来源。",
        selected_source_document_id: documentId,
        selected_source_version_id: versionId,
      }),
    );

    const refreshed = {
      ...detail,
      review: {
        ...detail.review,
        note: "另一位管理员刚刚更新的复核。",
        review_version: 3,
        reviewed_feedback_revision: 2,
        new_feedback_pending: false,
      },
    };
    review.mockRejectedValueOnce(
      new ApiError(409, {
        error: { code: "CONFLICT", message: "反馈复核已被更新。" },
      }),
    );
    detailRead.mockResolvedValueOnce(refreshed);
    await user.clear(note);
    await user.type(note, "这次保存会产生版本冲突。");
    await user.click(screen.getByRole("button", { name: "保存复核" }));

    expect(
      await screen.findByText("另一位管理员已更新此待办，页面已刷新为最新版本。"),
    ).toBeVisible();
    expect(note).toHaveValue("另一位管理员刚刚更新的复核。");
    expect(review).toHaveBeenLastCalledWith(
      traceId,
      expect.objectContaining({ expected_version: 2 }),
    );
  });
});
