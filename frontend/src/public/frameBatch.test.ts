import { afterEach, expect, it, vi } from "vitest";
import { createFrameBatch } from "./frameBatch";

afterEach(() => vi.unstubAllGlobals());

it("一帧内保留增量顺序，终态前主动flush不丢失最后片段", () => {
  const callbacks = new Map<number, FrameRequestCallback>();
  const schedule = vi.fn((callback: FrameRequestCallback) => {
    callbacks.set(1, callback);
    return 1;
  });
  vi.stubGlobal("requestAnimationFrame", schedule);
  vi.stubGlobal("cancelAnimationFrame", (id: number) => callbacks.delete(id));
  const apply = vi.fn<(items: string[]) => void>();
  const batch = createFrameBatch(apply);
  batch.push("第一段");
  batch.push("第二段");
  expect(schedule).toHaveBeenCalledOnce();
  expect(apply).not.toHaveBeenCalled();
  batch.flush();
  expect(apply).toHaveBeenCalledExactlyOnceWith(["第一段", "第二段"]);
  expect(callbacks.size).toBe(0);
  batch.flush();
  expect(apply).toHaveBeenCalledOnce();
});

it("取消旧会话后不会把待处理内容提交到新会话", () => {
  const callbacks = new Map<number, FrameRequestCallback>();
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callbacks.set(1, callback);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => callbacks.delete(id));
  const apply = vi.fn();
  const batch = createFrameBatch<string>(apply);
  batch.push("旧会话");
  batch.cancel();
  batch.flush();
  expect(apply).not.toHaveBeenCalled();
  expect(callbacks.size).toBe(0);
});
