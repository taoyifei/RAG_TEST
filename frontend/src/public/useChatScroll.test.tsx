import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useChatScroll } from "./useChatScroll";

afterEach(() => vi.unstubAllGlobals());

it("贴底跟随新内容，上翻后保持阅读位置，点击回到最新再恢复跟随", () => {
  const frames = new Map<number, FrameRequestCallback>();
  let nextId = 0;
  let resize: () => void = () => undefined;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    frames.set(++nextId, callback);
    return nextId;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: () => void) {
        resize = callback;
      }
      observe() {
        /* 模拟浏览器内容尺寸通知。 */
      }
      disconnect() {
        /* 清理观察。 */
      }
    },
  );
  function Fixture({ count = 1 }: { count?: number }) {
    const { scrollRef, contentRef, onScroll, awayFromLatest, jumpToLatest } =
      useChatScroll("conversation-1", count);
    return (
      <>
        <div data-testid="scroll" ref={scrollRef} onScroll={onScroll}>
          <div ref={contentRef}>回答</div>
        </div>
        {awayFromLatest && <button onClick={jumpToLatest}>回到最新</button>}
      </>
    );
  }
  const view = render(<Fixture />);
  const viewport = screen.getByTestId("scroll");
  Object.defineProperties(viewport, {
    scrollHeight: { value: 1000, configurable: true },
    clientHeight: { value: 200 },
  });
  const flush = () =>
    act(() => {
      const pending = [...frames.values()];
      frames.clear();
      for (const frame of pending) frame(0);
    });
  flush();
  expect(viewport.scrollTop).toBe(800);
  viewport.scrollTop = 200;
  fireEvent.scroll(viewport);
  expect(screen.getByRole("button", { name: "回到最新" })).toBeVisible();
  act(() => resize());
  flush();
  expect(viewport.scrollTop).toBe(200);
  fireEvent.click(screen.getByRole("button", { name: "回到最新" }));
  flush();
  expect(viewport.scrollTop).toBe(800);
  Object.defineProperty(viewport, "scrollHeight", { value: 1200 });
  act(() => resize());
  flush();
  expect(viewport.scrollTop).toBe(1000);
  viewport.scrollTop = 100;
  fireEvent.scroll(viewport);
  view.rerender(<Fixture count={2} />);
  flush();
  expect(viewport.scrollTop).toBe(1000);
});
