import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { ReferenceDrawer } from "./ReferenceDrawer";
import type { PublicCitation } from "./publicSse";

const citation: PublicCitation = {
  document_name: "委托单操作指引.docx",
  locator: "新建委托单 / 操作入口",
  quote: "从供应链菜单发起线上委托单。",
  reference_id: "ref-private-handle",
  source_kind: "weknora",
  source_available: true,
};

function Harness({
  reader,
  source = citation,
  onOutside = vi.fn(),
}: {
  reader: (referenceId: string) => Promise<string>;
  source?: PublicCitation;
  onOutside?: () => void;
}) {
  const [trigger, setTrigger] = useState<HTMLElement>();
  return (
    <div className="wst-root" data-theme="light" data-testid="theme-root">
      <main data-testid="background">
        <p>已有回答保持可见</p>
        <button
          onClick={(event) => setTrigger(event.currentTarget)}
          type="button"
        >
          阅读这条来源
        </button>
        <button onClick={onOutside} type="button">
          继续其他操作
        </button>
      </main>
      {trigger && (
        <ReferenceDrawer
          citation={source}
          onClose={() => setTrigger(undefined)}
          onReadReference={reader}
          returnFocusTo={trigger}
        />
      )}
    </div>
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.style.overflow = "";
});

it("保留真实文档名、定位和引文，原文格式可读且不执行HTML或展示内部句柄", async () => {
  const user = userEvent.setup();
  const raw =
    '## 办理步骤\n\n这里是**原文重点**。\n\n![图示](resource://hidden-source-image)\n\n<script>不要执行</script>\n<img src="https://example.test/extra.png">';
  const reader = vi.fn().mockResolvedValue(raw);
  render(<Harness reader={reader} />);
  await user.click(screen.getByRole("button", { name: "阅读这条来源" }));
  const dialog = screen.getByRole("dialog", {
    name: "来源阅读：委托单操作指引.docx",
  });
  expect(dialog.parentElement?.parentElement).toBe(
    screen.getByTestId("theme-root"),
  );
  expect(
    within(dialog).getByRole("heading", { name: "委托单操作指引.docx" }),
  ).toBeVisible();
  expect(within(dialog).getByText(citation.locator!)).toBeVisible();
  expect(within(dialog).getByText(citation.quote!)).toBeVisible();
  expect(
    await within(dialog).findByRole("heading", { name: "办理步骤" }),
  ).toBeVisible();
  expect(within(dialog).getByText("原文重点").tagName).toBe("STRONG");
  expect(dialog.textContent).toContain("图片请在原回答或原件中查看");
  expect(dialog.textContent).not.toMatch(
    /resource:\/\/|hidden-source-image|##|\*\*/,
  );
  expect(dialog.querySelector("script, img")).toBeNull();
  expect(dialog.textContent).not.toContain(citation.reference_id);
  expect(reader).toHaveBeenCalledExactlyOnceWith(citation.reference_id);
  expect(screen.getByText("已有回答保持可见")).toBeVisible();
  expect(dialog).not.toHaveAttribute("aria-modal");
  expect(screen.getByTestId("background")).not.toHaveAttribute("inert");
});

it("引用片段与读到原文trim相同时只呈现一份引用原文", async () => {
  const text = "## 办公账号\n\n使用**网页端**办理。";
  const user = userEvent.setup();
  render(
    <Harness
      source={{ ...citation, quote: `  ${text}\n` }}
      reader={() => Promise.resolve(text)}
    />,
  );
  await user.click(screen.getByRole("button", { name: "阅读这条来源" }));
  await waitFor(() =>
    expect(
      screen.queryByRole("region", { name: "本次引用片段" }),
    ).not.toBeInTheDocument(),
  );
  const source = screen.getByRole("region", { name: "引用原文" });
  expect(
    within(source).getByRole("heading", { name: "办公账号" }),
  ).toBeVisible();
  expect(screen.getAllByText("网页端")).toHaveLength(1);
});

it("不同片段仍保留上下文；切换来源后忽略旧请求的迟到正文", async () => {
  let finishOld: (value: string) => void = () => undefined;
  const reader = vi.fn((referenceId: string) =>
    referenceId === citation.reference_id
      ? new Promise<string>((resolve) => {
          finishOld = resolve;
        })
      : Promise.resolve("## 新来源正文\n\n这是当前原文。"),
  );
  const user = userEvent.setup();
  const view = render(<Harness reader={reader} />);
  await user.click(screen.getByRole("button", { name: "阅读这条来源" }));
  await waitFor(() => expect(reader).toHaveBeenCalledOnce());
  view.rerender(
    <Harness
      reader={reader}
      source={{
        ...citation,
        reference_id: "second-reference",
        quote: "新的实际引用片段。",
      }}
    />,
  );
  expect(
    await screen.findByRole("heading", { name: "新来源正文" }),
  ).toBeVisible();
  await act(async () => {
    finishOld("不应出现的旧原文");
    await Promise.resolve();
  });
  expect(screen.getByText("新的实际引用片段。")).toBeVisible();
  expect(screen.getByRole("region", { name: "本次引用片段" })).toBeVisible();
  expect(screen.queryByText("不应出现的旧原文")).not.toBeInTheDocument();
  expect(reader).toHaveBeenCalledTimes(2);
});

it("加载和失败期间仍显示来源上下文，明确提示后可以重试", async () => {
  let reject: (error: Error) => void = () => undefined;
  const reader = vi
    .fn<(referenceId: string) => Promise<string>>()
    .mockImplementationOnce(
      () =>
        new Promise((_, fail) => {
          reject = fail;
        }),
    )
    .mockResolvedValue("重新读取后的引用原文");
  const user = userEvent.setup();
  render(<Harness reader={reader} />);
  await user.click(screen.getByRole("button", { name: "阅读这条来源" }));
  expect(screen.getByRole("status")).toHaveTextContent("正在读取引用原文");
  expect(screen.getByText(citation.locator!)).toBeVisible();
  reject(new Error("不应暴露的内部接口路径"));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "资料可能已更新或访问权限已变化",
  );
  expect(screen.queryByText("不应暴露的内部接口路径")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重新读取" }));
  expect(await screen.findByText("重新读取后的引用原文")).toBeVisible();
  expect(reader).toHaveBeenCalledTimes(2);
});

it("父回调变化不会反复读取，显式重试使用最新回调", async () => {
  const first = vi.fn().mockRejectedValue(new Error("temporary"));
  const second = vi.fn().mockResolvedValue("最新回调读取的原文");
  const user = userEvent.setup();
  const view = render(<Harness reader={first} />);
  await user.click(screen.getByRole("button", { name: "阅读这条来源" }));
  await screen.findByRole("alert");
  view.rerender(<Harness reader={second} />);
  expect(first).toHaveBeenCalledOnce();
  expect(second).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "重新读取" }));
  expect(await screen.findByText("最新回调读取的原文")).toBeVisible();
  expect(second).toHaveBeenCalledOnce();
});

it("缺少原文时只展示实际片段，不请求不可用来源或编造页码", async () => {
  const reader = vi.fn().mockResolvedValue("不应该读取");
  const user = userEvent.setup();
  render(
    <Harness
      reader={reader}
      source={{ ...citation, source_available: false, locator: undefined }}
    />,
  );
  await user.click(screen.getByRole("button", { name: "阅读这条来源" }));
  expect(screen.getByText(citation.quote!)).toBeVisible();
  expect(
    screen.getByText("这条引用没有可读取的原文，可以先参考本次片段。"),
  ).toBeVisible();
  expect(reader).not.toHaveBeenCalled();
  expect(screen.queryByText(/第.*页/)).not.toBeInTheDocument();
});

it("桌面外部操作一次完成，关闭不会把焦点抢回来源按钮", async () => {
  const outside = vi.fn();
  const user = userEvent.setup();
  render(
    <Harness reader={() => Promise.resolve("原文")} onOutside={outside} />,
  );
  await user.click(screen.getByRole("button", { name: "阅读这条来源" }));
  await screen.findByText("原文");
  const button = screen.getByRole("button", { name: "继续其他操作" });
  await user.click(button);
  expect(outside).toHaveBeenCalledOnce();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(button).toHaveFocus();
});

it("窄屏隔离背景和锁定滚动，Tab与Esc关闭后恢复原焦点和滚动设置", async () => {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
  document.body.style.overflow = "scroll";
  const user = userEvent.setup();
  render(<Harness reader={() => Promise.reject(new Error("temporary"))} />);
  const trigger = screen.getByRole("button", { name: "阅读这条来源" });
  await user.click(trigger);
  const dialog = screen.getByRole("dialog");
  expect(dialog).toHaveAttribute("aria-modal", "true");
  expect(document.body.style.overflow).toBe("hidden");
  expect(screen.getByTestId("background")).toHaveAttribute("inert");
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "关闭来源阅读" })).toHaveFocus();
  await user.tab({ shift: true });
  expect(screen.getByRole("button", { name: "重新读取" })).toHaveFocus();
  await user.tab();
  expect(screen.getByRole("button", { name: "关闭来源阅读" })).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByTestId("background")).not.toHaveAttribute("inert");
  expect(document.body.style.overflow).toBe("scroll");
  await waitFor(() => expect(trigger).toHaveFocus());
});
