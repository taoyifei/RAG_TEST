import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QuestionAnalyticsSection } from "./QuestionAnalyticsSection";
import {
  wanshitongAdminApi,
  type QuestionAnalyticsItem,
  type QuestionAnalyticsPage,
  type QuestionAnalyticsRun,
} from "./adminApi";

const traceId = `trace_${"a".repeat(32)}`;
const completeRun: QuestionAnalyticsRun = {
  run_id: "run_20260923",
  state: "COMPLETE",
  deployment_id: "08r-test",
  window_start: "2026-09-16T00:00:00Z",
  window_end: "2026-09-23T00:00:00Z",
  observed_at: "2026-09-23T00:00:01Z",
  source_total: 25,
  eligible_count: 21,
  unknown_count: 1,
  excluded_count: 2,
  unknown_identity_count: 3,
  body_missing_count: 1,
  unreadable_count: 0,
  pending_count: 1,
  created_at: "2026-09-23T00:00:02Z",
  finished_at: "2026-09-23T00:00:03Z",
  expires_at: null,
  failure_code: null,
  coverage_start: "2026-09-22T00:00:00Z",
};

const item: QuestionAnalyticsItem = {
  group_key: "group_one",
  group_kind: "EXACT",
  representative_question: "材料怎么提交？",
  request_count: 21,
  distinct_users: 2,
  user_day_heat: 2,
  manual_request_count: 20,
  manual_distinct_users: 2,
  manual_user_day_heat: 2,
  suggestion_count: 1,
  popular_count: 0,
  retry_count: 0,
  unknown_entry_count: 0,
  answered_count: 18,
  refused_count: 2,
  failed_count: 1,
  feedback_count: 0,
  helpful_count: 0,
  negative_feedback_count: 0,
  false_refusal_count: 0,
  confirmed_open_issue_count: 0,
  last_seen_at: "2026-09-22T12:00:00Z",
  sample_trace_ids: [traceId],
};

const firstPage: QuestionAnalyticsPage = {
  run: completeRun,
  latest_run: completeRun,
  items: [item],
  total: 21,
  next_offset: 20,
};

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("问题运营榜", () => {
  it("展示覆盖率和未评价，分页、切榜及样本沿已有详情入口", async () => {
    const list = vi
      .spyOn(wanshitongAdminApi, "questionAnalytics")
      .mockImplementation((board, offset) =>
        Promise.resolve(
          board === "frequent" && offset === 0
            ? firstPage
            : { ...firstPage, items: [], next_offset: null },
        ),
      );
    const samples = vi
      .spyOn(wanshitongAdminApi, "questionAnalyticsSamples")
      .mockResolvedValue({
        items: [
          {
            trace_id: traceId,
            question: "材料怎么提交？",
            created_at: "2026-09-22T12:00:00Z",
            status: "ANSWERED",
            has_feedback: true,
          },
        ],
      });
    const inspect = vi.fn();
    const user = userEvent.setup();
    render(<QuestionAnalyticsSection onInspectFeedback={inspect} />);

    expect(await screen.findByText("材料怎么提交？")).toBeVisible();
    expect(screen.getByText(/有效 21 \/ 来源 25/)).toBeVisible();
    expect(screen.getByText(/约 1.0 天/)).toBeVisible();
    expect(screen.getByText("帮助率 未评价")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "查看样本" }));
    expect(
      await screen.findByRole("link", { name: "查看问答历史" }),
    ).toHaveAttribute("href", `/admin/history?trace_id=${traceId}`);
    expect(samples).toHaveBeenCalledWith(
      completeRun.run_id,
      item.group_key,
      expect.any(AbortSignal),
    );
    await user.click(screen.getByRole("button", { name: "查看反馈详情" }));
    expect(inspect).toHaveBeenCalledWith(traceId);
    await user.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() =>
      expect(list).toHaveBeenCalledWith(
        "frequent",
        20,
        expect.any(AbortSignal),
      ),
    );
    await user.click(screen.getByRole("button", { name: "高频但答不好" }));
    await waitFor(() =>
      expect(list).toHaveBeenCalledWith(
        "unresolved",
        0,
        expect.any(AbortSignal),
      ),
    );
  });

  it("刷新任务运行和失败时保留上一份完整快照", async () => {
    const building = {
      ...completeRun,
      run_id: "run_new",
      state: "BUILDING" as const,
    };
    const failed = {
      ...building,
      state: "FAILED" as const,
      failure_code: "READ_LIMIT",
    };
    vi.spyOn(wanshitongAdminApi, "questionAnalytics")
      .mockResolvedValueOnce(firstPage)
      .mockResolvedValue({ ...firstPage, latest_run: failed });
    vi.spyOn(wanshitongAdminApi, "refreshQuestionAnalytics").mockResolvedValue(
      building,
    );
    const poll = vi
      .spyOn(wanshitongAdminApi, "questionAnalyticsRun")
      .mockRejectedValueOnce(new Error("暂时无法读取状态"))
      .mockResolvedValue(failed);
    render(<QuestionAnalyticsSection onInspectFeedback={vi.fn()} />);
    expect(await screen.findByText("材料怎么提交？")).toBeVisible();

    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "重新统计" }));
      await Promise.resolve();
    });
    expect(screen.getByText(/新统计正在运行/)).toBeVisible();
    expect(screen.getByText("材料怎么提交？")).toBeVisible();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(poll).toHaveBeenCalledWith(building.run_id, expect.any(AbortSignal));
    expect(screen.getByText("暂时无法读取状态")).toBeVisible();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(poll).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/READ_LIMIT/)).toBeVisible();
    expect(screen.queryByText("暂时无法读取状态")).not.toBeInTheDocument();
    expect(screen.getByText("材料怎么提交？")).toBeVisible();
  });

  it("状态读取连续失败后停止轮询，并可手动重取运行状态", async () => {
    const building = {
      ...completeRun,
      run_id: "run_retry",
      state: "BUILDING" as const,
    };
    const completed = { ...building, state: "COMPLETE" as const };
    const list = vi
      .spyOn(wanshitongAdminApi, "questionAnalytics")
      .mockResolvedValueOnce(firstPage)
      .mockResolvedValueOnce({ ...firstPage, latest_run: building })
      .mockResolvedValue({
        ...firstPage,
        run: completed,
        latest_run: completed,
      });
    vi.spyOn(wanshitongAdminApi, "refreshQuestionAnalytics").mockResolvedValue(
      building,
    );
    const poll = vi
      .spyOn(wanshitongAdminApi, "questionAnalyticsRun")
      .mockRejectedValueOnce(new Error("暂时无法读取状态"))
      .mockRejectedValueOnce(new Error("暂时无法读取状态"))
      .mockRejectedValueOnce(new Error("暂时无法读取状态"))
      .mockResolvedValueOnce(completed);
    render(<QuestionAnalyticsSection onInspectFeedback={vi.fn()} />);
    expect(await screen.findByText("材料怎么提交？")).toBeVisible();

    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "重新统计" }));
      await Promise.resolve();
    });
    for (let index = 0; index < 3; index += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
    }
    expect(poll).toHaveBeenCalledTimes(3);
    expect(screen.getByText(/任务状态连续读取失败/)).toBeVisible();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(poll).toHaveBeenCalledTimes(3);
    expect(screen.getByText("材料怎么提交？")).toBeVisible();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "刷新显示" }));
      await Promise.resolve();
    });
    expect(list).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(poll).toHaveBeenCalledTimes(4);
    expect(screen.queryByText(/任务状态连续读取失败/)).not.toBeInTheDocument();
  });

  it("空榜和失败刷新明确标记，不把失败任务当作统计结果", async () => {
    const failed = {
      ...completeRun,
      state: "FAILED" as const,
      failure_code: "READ_LIMIT",
    };
    vi.spyOn(wanshitongAdminApi, "questionAnalytics").mockResolvedValue({
      run: null,
      latest_run: failed,
      items: [],
      total: 0,
      next_offset: null,
    });
    render(<QuestionAnalyticsSection onInspectFeedback={vi.fn()} />);
    expect(await screen.findByText("尚无完整统计快照")).toBeVisible();
    expect(screen.getByText(/READ_LIMIT/)).toBeVisible();
    expect(screen.queryByText("材料怎么提交？")).not.toBeInTheDocument();
  });
});
