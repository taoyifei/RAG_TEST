import { afterEach, expect, it, vi } from "vitest";

import { setBrowserCsrfToken } from "../../api/client";
import { wanshitongAdminApi } from "./adminApi";

afterEach(() => {
  setBrowserCsrfToken("");
  vi.restoreAllMocks();
});

it("Trace 单条与批量下载使用固定范围接口及管理员会话", async () => {
  setBrowserCsrfToken("synthetic-csrf");
  const traceId = `trace_${"a".repeat(32)}`;
  const fetchMock = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(
      new Response("{}", {
        headers: {
          "Content-Disposition": `attachment; filename="${traceId}.json"`,
        },
      }),
    )
    .mockResolvedValueOnce(
      new Response("zip", {
        headers: {
          "Content-Disposition": 'attachment; filename="operational-traces.zip"',
        },
      }),
    );

  const single = await wanshitongAdminApi.exportOperationalTrace(traceId);
  const batch = await wanshitongAdminApi.exportOperationalTraces([traceId]);

  expect(single.filename).toBe(`${traceId}.json`);
  expect(batch.filename).toBe("operational-traces.zip");
  expect(fetchMock.mock.calls[0][0]).toBe(
    `/api/v1/admin/wanshitong/operational-traces/${traceId}/export`,
  );
  expect(fetchMock.mock.calls[0][1]?.credentials).toBe("same-origin");
  expect(fetchMock.mock.calls[1][0]).toBe(
    "/api/v1/admin/wanshitong/operational-traces:export",
  );
  expect(fetchMock.mock.calls[1][1]?.body).toBe(
    JSON.stringify({ trace_ids: [traceId] }),
  );
  const headers = new Headers(fetchMock.mock.calls[1][1]?.headers);
  expect(headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(headers.has("Authorization")).toBe(false);
});

it("问答批量下载走湾事通固定范围接口并请求完整原文", async () => {
  setBrowserCsrfToken("synthetic-csrf");
  const ids = [`trace_${"1".repeat(32)}`, `trace_${"2".repeat(32)}`];
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response("zip", {
      headers: {
        "Content-Disposition": 'attachment; filename="history-traces.zip"',
      },
    }),
  );
  const file = await wanshitongAdminApi.exportHistoryTraces(ids);
  expect(file.filename).toBe("history-traces.zip");
  expect(fetchMock.mock.calls[0][0]).toBe(
    "/api/v1/admin/wanshitong/history-traces:export",
  );
  expect(fetchMock.mock.calls[0][1]?.body).toBe(
    JSON.stringify({ trace_ids: ids }),
  );
  const headers = new Headers(fetchMock.mock.calls[0][1]?.headers);
  expect(headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
});

it("反馈复核与安全导出复用管理员会话和 CSRF", async () => {
  setBrowserCsrfToken("synthetic-csrf");
  const traceId = `trace_${"3".repeat(32)}`;
  const reviewBody = {
    expected_version: 2,
    review_status: "REVIEWED" as const,
    root_cause: "WRONG_SOURCE",
    note: "确认来源错误。",
    selected_source_document_id: null,
    selected_source_version_id: null,
    evaluation_candidate: true,
    fix_reference: null,
    verification_references: [],
  };
  const fetchMock = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response("{}"))
    .mockResolvedValueOnce(
      new Response("{}", {
        headers: {
          "Content-Disposition":
            'attachment; filename="wanshitong-feedback.json"',
        },
      }),
    );

  await wanshitongAdminApi.reviewFeedback(traceId, reviewBody);
  const file = await wanshitongAdminApi.exportFeedback([traceId]);

  expect(fetchMock.mock.calls[0][0]).toBe(
    `/api/v1/admin/wanshitong/feedback/${traceId}/review`,
  );
  expect(fetchMock.mock.calls[0][1]?.method).toBe("PATCH");
  expect(fetchMock.mock.calls[0][1]?.body).toBe(JSON.stringify(reviewBody));
  const reviewHeaders = new Headers(fetchMock.mock.calls[0][1]?.headers);
  expect(reviewHeaders.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(reviewHeaders.has("Authorization")).toBe(false);
  expect(fetchMock.mock.calls[1][0]).toBe(
    "/api/v1/admin/wanshitong/feedback/export",
  );
  expect(fetchMock.mock.calls[1][1]?.body).toBe(
    JSON.stringify({ trace_ids: [traceId] }),
  );
  expect(file.filename).toBe("wanshitong-feedback.json");
});

it("问题运营榜分页、刷新、任务轮询和样本都走固定范围接口", async () => {
  setBrowserCsrfToken("synthetic-csrf");
  const runId = "run_20260923";
  const groupKey = "group/a+b";
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(() =>
    Promise.resolve(new Response(JSON.stringify({ run_id: runId, items: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })),
  );

  await wanshitongAdminApi.questionAnalytics("unresolved", 20);
  await wanshitongAdminApi.refreshQuestionAnalytics();
  await wanshitongAdminApi.questionAnalyticsRun(runId);
  await wanshitongAdminApi.questionAnalyticsSamples(runId, groupKey);

  expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
    "/api/v1/admin/wanshitong/question-analytics?board=unresolved&page_size=20&offset=20",
    "/api/v1/admin/wanshitong/question-analytics:refresh",
    `/api/v1/admin/wanshitong/question-analytics/runs/${runId}`,
    "/api/v1/admin/wanshitong/question-analytics/samples?run_id=run_20260923&group_key=group%2Fa%2Bb",
  ]);
  expect(fetchMock.mock.calls[1][1]?.method).toBe("POST");
  expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get("X-CSRF-Token"))
    .toBe("synthetic-csrf");
  for (const [, init] of fetchMock.mock.calls) {
    expect(init?.credentials).toBe("same-origin");
  }
});
