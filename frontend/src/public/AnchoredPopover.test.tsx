import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AnchoredPopover } from "./AnchoredPopover";

function rect(
  left: number,
  top: number,
  width: number,
  height: number,
): DOMRect {
  return {
    x: left,
    y: top,
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    toJSON: () => ({}),
  };
}

let anchor = rect(80, 60, 180, 40);
let contentHeight = 200;
let frameId = 0;
let frames = new Map<number, FrameRequestCallback>();
let observerUpdates: (() => void)[] = [];
let disconnects: ReturnType<typeof vi.fn>[] = [];

function renderedHeight(element: HTMLElement): number {
  const limit = Number.parseFloat(element.style.maxHeight);
  return Number.isFinite(limit)
    ? Math.min(contentHeight, limit)
    : contentHeight;
}

function flushFrames() {
  act(() => {
    const pending = [...frames.values()];
    frames.clear();
    for (const callback of pending) callback(0);
  });
}

function Harness({
  theme = "light",
  preferredSide = "bottom",
  width = 304,
  onOutside = vi.fn(),
}: {
  theme?: "light" | "dark";
  preferredSide?: "top" | "bottom";
  width?: number;
  onOutside?: () => void;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  return (
    <div className="wst-root" data-testid="theme-root" data-theme={theme}>
      <div data-testid="clipping-parent" style={{ overflow: "hidden" }}>
        <button
          aria-expanded={open}
          data-testid="trigger"
          onClick={() => setOpen((value) => !value)}
          ref={trigger}
          type="button"
        >
          打开范围
        </button>
        <AnchoredPopover
          className="scope-options"
          label="可选范围"
          onDismiss={() => setOpen(false)}
          open={open}
          preferredSide={preferredSide}
          role="group"
          triggerRef={trigger}
          width={width}
        >
          <button type="button">第一项</button>
          <button type="button">最后一项</button>
        </AnchoredPopover>
        <button onClick={onOutside} type="button">
          下一项操作
        </button>
      </div>
    </div>
  );
}

beforeEach(() => {
  anchor = rect(80, 60, 180, 40);
  contentHeight = 200;
  frameId = 0;
  frames = new Map();
  observerUpdates = [];
  disconnects = [];
  vi.stubGlobal("innerWidth", 800);
  vi.stubGlobal("innerHeight", 600);
  vi.stubGlobal("visualViewport", undefined);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    frames.set(++frameId, callback);
    return frameId;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  vi.stubGlobal(
    "ResizeObserver",
    class implements ResizeObserver {
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
      constructor(callback: ResizeObserverCallback) {
        observerUpdates.push(() => callback([], this));
        disconnects.push(this.disconnect);
      }
    },
  );
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function (this: HTMLElement) {
      if (this.dataset.testid === "trigger") return anchor;
      return rect(0, 0, 0, 0);
    },
  );
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(
    function (this: HTMLElement) {
      return this.classList.contains("wst-anchored-popover")
        ? contentHeight
        : 0;
    },
  );
  for (const property of ["clientHeight", "offsetHeight"] as const) {
    vi.spyOn(HTMLElement.prototype, property, "get").mockImplementation(
      function (this: HTMLElement) {
        return this.classList.contains("wst-anchored-popover")
          ? renderedHeight(this)
          : 0;
      },
    );
  }
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("跟随触发器的浮层", () => {
  it.each(["light", "dark"] as const)(
    "%s 主题下直接挂到主题根节点，避开 overflow 裁剪父级",
    async (theme) => {
      const user = userEvent.setup();
      render(<Harness theme={theme} />);
      await user.click(screen.getByRole("button", { name: "打开范围" }));
      const popover = screen.getByRole("group", { name: "可选范围" });
      expect(popover.parentElement).toBe(screen.getByTestId("theme-root"));
      expect(popover.closest("[data-theme]")).toHaveAttribute(
        "data-theme",
        theme,
      );
      expect(screen.getByTestId("clipping-parent")).not.toContainElement(
        popover,
      );
      expect(popover).toHaveClass("wst-anchored-popover", "scope-options");
      expect(popover).toHaveStyle({ position: "fixed", visibility: "visible" });
    },
  );

  it("靠近顶部时从首选向上改为向下展开", async () => {
    const user = userEvent.setup();
    render(<Harness preferredSide="top" />);
    await user.click(screen.getByRole("button", { name: "打开范围" }));
    const popover = screen.getByRole("group", { name: "可选范围" });
    expect(popover).toHaveAttribute("data-side", "bottom");
    expect(popover).toHaveStyle({
      left: "80px",
      top: "108px",
      maxHeight: "480px",
    });
  });

  it("靠近底部时从首选向下改为向上展开", async () => {
    anchor = rect(80, 500, 180, 40);
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "打开范围" }));
    const popover = screen.getByRole("group", { name: "可选范围" });
    expect(popover).toHaveAttribute("data-side", "top");
    expect(popover).toHaveStyle({ top: "292px", maxHeight: "480px" });
  });

  it("两侧都放不下时选择较大空间并在框内滚动", async () => {
    contentHeight = 700;
    anchor = rect(80, 250, 180, 40);
    const user = userEvent.setup();
    render(<Harness preferredSide="top" />);
    await user.click(screen.getByRole("button", { name: "打开范围" }));
    const popover = screen.getByRole("group", { name: "可选范围" });
    expect(popover).toHaveAttribute("data-side", "bottom");
    expect(popover).toHaveStyle({
      top: "298px",
      maxHeight: "290px",
      overflowY: "auto",
    });
    expect(
      Number.parseFloat(popover.style.top) +
        Number.parseFloat(popover.style.maxHeight),
    ).toBeLessThanOrEqual(588);
  });

  it.each([
    [220, 232, 12],
    [-40, 800, 12],
    [700, 800, 484],
  ])(
    "窄屏或横向靠边时限位：触发器 x=%s，视口宽=%s",
    async (left, width, expected) => {
      vi.stubGlobal("innerWidth", width);
      anchor = rect(left, 60, 20, 40);
      const user = userEvent.setup();
      render(<Harness />);
      await user.click(screen.getByRole("button", { name: "打开范围" }));
      const popover = screen.getByRole("group", { name: "可选范围" });
      const resolvedWidth = Math.min(304, width - 24);
      expect(popover).toHaveStyle({
        left: `${expected}px`,
        width: `${resolvedWidth}px`,
      });
      expect(expected + resolvedWidth).toBeLessThanOrEqual(width - 12);
    },
  );

  it("可视视口偏移和软键盘缩小时重新定位，不超出实际可见区域", async () => {
    const viewport = Object.assign(new EventTarget(), {
      width: 360,
      height: 420,
      offsetLeft: 30,
      offsetTop: 100,
    });
    vi.stubGlobal("visualViewport", viewport);
    anchor = rect(350, 350, 20, 40);
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "打开范围" }));
    const popover = screen.getByRole("group", { name: "可选范围" });
    expect(popover).toHaveStyle({ left: "74px", top: "142px" });

    viewport.height = 260;
    viewport.offsetTop = 140;
    viewport.dispatchEvent(new Event("resize"));
    flushFrames();
    expect(popover).toHaveStyle({
      left: "74px",
      top: "152px",
      maxHeight: "190px",
    });
    viewport.offsetLeft = 100;
    viewport.dispatchEvent(new Event("scroll"));
    flushFrames();
    expect(popover).toHaveStyle({ left: "144px" });
  });

  it("容器滚动、窗口缩放、内容尺寸变化会重算，卸载清理监听和待执行帧", async () => {
    const user = userEvent.setup();
    const view = render(<Harness />);
    await user.click(screen.getByRole("button", { name: "打开范围" }));
    const popover = screen.getByRole("group", { name: "可选范围" });
    anchor = rect(100, 480, 180, 40);
    fireEvent.scroll(screen.getByTestId("clipping-parent"));
    flushFrames();
    expect(popover).toHaveStyle({ left: "100px", top: "272px" });

    vi.stubGlobal("innerHeight", 400);
    anchor = rect(100, 200, 180, 40);
    fireEvent.resize(window);
    flushFrames();
    expect(popover).toHaveAttribute("data-side", "top");
    expect(popover).toHaveStyle({ top: "12px", maxHeight: "180px" });

    contentHeight = 80;
    act(() => observerUpdates.forEach((update) => update()));
    flushFrames();
    expect(popover).toHaveAttribute("data-side", "bottom");
    expect(popover).toHaveStyle({ top: "248px" });
    fireEvent.scroll(document);
    expect(frames.size).toBeGreaterThan(0);
    view.unmount();
    expect(frames.size).toBe(0);
    expect(
      disconnects.every((disconnect) => disconnect.mock.calls.length > 0),
    ).toBe(true);
    fireEvent.resize(window);
    expect(frames.size).toBe(0);
  });

  it("portal 保持原 Tab 顺序，框内点击不关，外部点击一次完成且 Esc 还焦点", async () => {
    const user = userEvent.setup();
    const outside = vi.fn();
    render(<Harness onOutside={outside} />);
    const trigger = screen.getByRole("button", { name: "打开范围" });
    await user.click(trigger);
    await user.tab();
    expect(screen.getByRole("button", { name: "第一项" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(trigger).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "最后一项" }));
    expect(screen.getByRole("group", { name: "可选范围" })).toBeVisible();
    await user.tab();
    expect(
      screen.queryByRole("group", { name: "可选范围" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一项操作" })).toHaveFocus();

    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "下一项操作" }));
    expect(outside).toHaveBeenCalledOnce();
    expect(
      screen.queryByRole("group", { name: "可选范围" }),
    ).not.toBeInTheDocument();
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "第一项" }));
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    expect(
      screen.queryByRole("group", { name: "可选范围" }),
    ).not.toBeInTheDocument();
  });
});
