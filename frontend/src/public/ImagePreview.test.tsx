import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { ImagePreview } from "./ImagePreview";

const IMAGE =
  "/kb/api/public/conversations/example/turns/example/resources?file_path=resource%3A%2F%2Ffigures%2Fguide.png";

function Harness() {
  const [trigger, setTrigger] = useState<HTMLElement>();
  return (
    <div className="wst-root" data-theme="dark" data-testid="theme-root">
      <main data-testid="background">
        <button
          onClick={(event) => setTrigger(event.currentTarget)}
          type="button"
        >
          放大操作示意图
        </button>
      </main>
      {trigger && (
        <ImagePreview
          src={IMAGE}
          alt="操作示意图"
          onClose={() => setTrigger(undefined)}
          returnFocusTo={trigger}
        />
      )}
    </div>
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.style.overflow = "";
});

it("放大复用已有受保护图片地址并继承主题，Esc关闭后恢复焦点", async () => {
  const user = userEvent.setup();
  render(<Harness />);
  const trigger = screen.getByRole("button", { name: "放大操作示意图" });
  await user.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "图片预览" });
  expect(dialog).toHaveAttribute("aria-modal", "true");
  expect(dialog.closest("[data-theme]")).toHaveAttribute("data-theme", "dark");
  expect(dialog.parentElement?.parentElement).toBe(
    screen.getByTestId("theme-root"),
  );
  const image = within(dialog).getByRole("img", { name: "操作示意图" });
  expect(image).toHaveAttribute("src", IMAGE);
  expect(image).toHaveAttribute("referrerpolicy", "no-referrer");
  expect(screen.getByRole("status")).toHaveTextContent("正在显示图片");
  fireEvent.load(image);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(document.body.style.overflow).toBe("hidden");
  expect(screen.getByTestId("background")).toHaveAttribute("inert");
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(document.body.style.overflow).toBe("");
  expect(screen.getByTestId("background")).not.toHaveAttribute("inert");
  await waitFor(() => expect(trigger).toHaveFocus());
});

it("图片读取失败有明确重试，仍只使用原地址", async () => {
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByRole("button", { name: "放大操作示意图" }));
  const original = screen.getByRole("img", { name: "操作示意图" });
  fireEvent.error(original);
  expect(screen.getByRole("alert")).toHaveTextContent("图片暂时无法显示");
  await user.click(screen.getByRole("button", { name: "重新加载图片" }));
  const retry = screen.getByRole("img", { name: "操作示意图" });
  expect(retry).not.toBe(original);
  expect(retry).toHaveAttribute("src", IMAGE);
  fireEvent.load(retry);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

it("遮罩可触屏关闭，不关闭预览里的点击，键盘焦点留在预览内", async () => {
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByRole("button", { name: "放大操作示意图" }));
  const close = screen.getByRole("button", { name: "关闭图片预览" });
  expect(close).toHaveFocus();
  await user.tab();
  expect(close).toHaveFocus();
  await user.click(screen.getByRole("img", { name: "操作示意图" }));
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  await user.pointer({
    keys: "[TouchA]",
    target: screen.getByRole("button", { name: "关闭图片预览遮罩" }),
  });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
