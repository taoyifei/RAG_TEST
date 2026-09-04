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
