import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useJobPolling } from "./use-job-polling";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("任务轮询退避", () => {
  it("页面隐藏时暂停请求，恢复可见后继续", async () => {
    vi.useFakeTimers();
    let visibility: DocumentVisibilityState = "hidden";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(
      () => visibility,
    );
    const load = vi.fn(() => Promise.resolve());
    renderHook(() => useJobPolling(load));

    await act(() => vi.advanceTimersByTimeAsync(3_000));
    expect(load).not.toHaveBeenCalled();
    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(load).toHaveBeenCalled();
  });
});

it("终态停止轮询，失败使用退避且报告错误", async () => {
  vi.useFakeTimers();
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  const failure = new Error("synthetic polling failure");
  const load = vi
    .fn<() => Promise<boolean>>()
    .mockRejectedValueOnce(failure)
    .mockResolvedValueOnce(true)
    .mockResolvedValue(false);
  const onError = vi.fn();
  renderHook(() => useJobPolling(load, onError));
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(onError).toHaveBeenCalledWith(failure);
  await act(() => vi.advanceTimersByTimeAsync(1200));
  expect(load).toHaveBeenCalledTimes(1);
  await act(() => vi.advanceTimersByTimeAsync(1200));
  expect(load).toHaveBeenCalledTimes(2);
  await act(() => vi.advanceTimersByTimeAsync(1200));
  expect(load).toHaveBeenCalledTimes(3);
  await act(() => vi.advanceTimersByTimeAsync(60000));
  expect(load).toHaveBeenCalledTimes(3);
});
