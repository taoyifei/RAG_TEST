import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { HistorySupportDownload } from "./HistorySupportDownload";

beforeEach(() => {
  vi.restoreAllMocks();
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
});

it("正文下载先告知敏感性并延迟释放 Object URL", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const blob = new Blob(["synthetic support"], {
    type: "application/zip",
  });
  vi.spyOn(api, "exportHistoryTrace").mockResolvedValue({
    blob,
    filename: "trace-safe-support.zip",
  });
  const createUrl = vi
    .spyOn(URL, "createObjectURL")
    .mockReturnValue("blob:synthetic");
  const revokeUrl = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {
    return undefined;
  });
  const setTimeout = vi.spyOn(window, "setTimeout");
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(() => undefined);

  render(
    <HistorySupportDownload traceIds={["trace_safe"]}>
      下载本条支持包
    </HistorySupportDownload>,
  );
  await user.click(screen.getByRole("button", { name: "下载本条支持包" }));
  await user.click(
    screen.getByRole("checkbox", {
      name: /包含当前仍获授权的敏感问题、答案与引用正文/,
    }),
  );
  expect(screen.getByRole("alert")).toHaveTextContent("可能含敏感问答");
  await user.click(screen.getByRole("button", { name: "确认下载支持包" }));

  expect(api.exportHistoryTrace).toHaveBeenCalledWith("trace_safe", true);
  expect(createUrl).toHaveBeenCalledWith(blob);
  expect(click).toHaveBeenCalledTimes(1);
  expect(setTimeout).toHaveBeenCalledWith(expect.any(Function), 1_000);
  expect(revokeUrl).not.toHaveBeenCalled();
  act(() => void vi.runOnlyPendingTimers());
  expect(revokeUrl).toHaveBeenCalledWith("blob:synthetic");
});

it("批量元数据下载保留排序后的选择且失败后恢复按钮", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  vi.spyOn(api, "exportHistoryTraces").mockRejectedValue(
    new Error("合成导出失败"),
  );
  render(
    <HistorySupportDownload traceIds={["trace_b", "trace_a"]}>
      下载已选支持包
    </HistorySupportDownload>,
  );
  await user.click(screen.getByRole("button", { name: "下载已选支持包" }));
  await user.click(screen.getByRole("button", { name: "确认下载支持包" }));

  expect(api.exportHistoryTraces).toHaveBeenCalledWith(
    ["trace_b", "trace_a"],
    false,
  );
  expect(await screen.findByText("合成导出失败")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "确认下载支持包" }),
  ).toBeEnabled();
});
