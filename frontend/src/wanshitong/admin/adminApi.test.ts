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
