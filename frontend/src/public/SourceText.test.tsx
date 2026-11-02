import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { SourceText } from "./SourceText";

afterEach(() => vi.unstubAllGlobals());

it("来源Markdown显示中文标题、强调、列表和可横向滚动的原生表格", () => {
  const { container } = render(
    <SourceText
      text={
        "## 办公账号\n\n使用**网页端**办理。\n\n1. 打开办公系统\n2. 按要求申请\n\n| 类型 | 说明 |\n| --- | --- |\n| 账号 | 入职后提供 |\n\n组织编码：00112233。\n\n[办公系统](http://oa.example/)与[联系经办人](mailto:owner@example.com)"
      }
    />,
  );
  expect(
    screen.getByRole("heading", { name: "办公账号", level: 2 }),
  ).toBeVisible();
  expect(screen.getByText("网页端").tagName).toBe("STRONG");
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(screen.getByRole("region", { name: "原文表格" })).toHaveAttribute(
    "tabindex",
    "0",
  );
  expect(screen.getByRole("table")).toBeVisible();
  expect(screen.getAllByRole("row")).toHaveLength(2);
  expect(screen.getByText("组织编码：00112233。")).toBeVisible();
  expect(screen.getByRole("link", { name: "办公系统" })).toHaveAttribute(
    "href",
    "http://oa.example/",
  );
  expect(screen.getByRole("link", { name: "联系经办人" })).toHaveAttribute(
    "href",
    "mailto:owner@example.com",
  );
  expect(container.textContent).not.toMatch(/##|\*\*/);
});

it("图片仅保留人话说明，不发出图片请求、执行HTML或泄漏链接/纯文本中的资源句柄", () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  const { container } = render(
    <SourceText
      text={
        '![内部图示](resource://secret-image)\n\n![外部图示](https://example.com/image.png)\n\n[私图](resource://secret-link) [脚本](javascript:alert) [调试文件](https://example.com/kb/api/resources?file_path=resource://secret-parameter)\n\n纯文本 resource://secret-text。\n\n`resource://secret-code`\n\n```text\nresource://secret-block\n```\n\n<script>不应执行</script>\n<img src="https://example.com/html.png" onerror="alert(1)">'
      }
    />,
  );
  expect(container.querySelector("img,script,iframe")).toBeNull();
  expect(container.textContent).toContain(
    "内部图示：图片请在原回答或原件中查看",
  );
  expect(container.textContent).toContain(
    "外部图示：图片请在原回答或原件中查看",
  );
  expect(container.textContent).not.toMatch(/resource:\/\/|secret-|不应执行/);
  expect(screen.queryByRole("link", { name: "私图" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "脚本" })).not.toBeInTheDocument();
  expect(
    screen.queryByRole("link", { name: "调试文件" }),
  ).not.toBeInTheDocument();
  expect(fetch).not.toHaveBeenCalled();
});

it("文档分块边界的孤立加粗标记不露出，代码示例的业务符号仍保留", () => {
  const { container } = render(
    <SourceText
      text={
        "上一段结束。**![](resource://hidden-orphan)\n\n**下一片段标题\n\n`a ** b`"
      }
    />,
  );
  expect(container.textContent).toContain("上一段结束。");
  expect(container.textContent).toContain("下一片段标题");
  expect(container.textContent).not.toMatch(
    /resource:\/\/|hidden-orphan|结束。\*\*|\*\*下一/,
  );
  expect(screen.getByText("a ** b").tagName).toBe("CODE");
});

it("引用独有配图走当前会话资源，链接中的图片可放大且不嵌套交互元素", async () => {
  const previous = window.location.href;
  window.history.replaceState({}, "", "/kb/");
  try {
    const preview = vi.fn();
    const { container } = render(
      <SourceText
        text="[**![菜单示意图](resource://figures/guide.png)**](https://example.test/guide)"
        imageContext={{ conversationId: "own/session", traceId: "own trace" }}
        onPreviewImage={preview}
      />,
    );
    const image = screen.getByRole("img", { name: "菜单示意图" });
    const src =
      "/kb/api/public/conversations/own%2Fsession/turns/own%20trace/resources?file_path=resource%3A%2F%2Ffigures%2Fguide.png";
    expect(image).toHaveAttribute("src", src);
    expect(image).toHaveAttribute("loading", "lazy");
    expect(image).toHaveAttribute("referrerpolicy", "no-referrer");
    const trigger = screen.getByRole("button", {
      name: "放大图片：菜单示意图",
    });
    await userEvent.setup().click(trigger);
    expect(preview).toHaveBeenCalledExactlyOnceWith({
      src,
      alt: "菜单示意图",
      trigger,
    });
    expect(container.querySelector("a button, button button")).toBeNull();
    expect(container.textContent).not.toContain("resource://");
  } finally {
    window.history.replaceState({}, "", previous);
  }
});

it("配图读取失败可直接重试，仍使用相同受保护地址", async () => {
  render(
    <SourceText
      text="![操作图](resource://private-figure)"
      imageContext={{ conversationId: "own", traceId: "trace" }}
      onPreviewImage={vi.fn()}
    />,
  );
  const image = screen.getByRole("img", { name: "操作图" });
  const src = image.getAttribute("src");
  fireEvent.error(image);
  expect(screen.getByRole("status")).toHaveTextContent("操作图暂时无法显示。");
  expect(
    screen.queryByRole("button", { name: "放大图片：操作图" }),
  ).not.toBeInTheDocument();
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "重新加载图片" }));
  expect(screen.getByRole("img", { name: "操作图" })).toHaveAttribute(
    "src",
    src,
  );
  expect(screen.getByRole("img", { name: "操作图" })).not.toBe(image);
});

it("已接会话也不直接请求外部或非消息资源地址，切换会话后不能复用旧图片地址", () => {
  const context = { conversationId: "first", traceId: "first-trace" };
  const preview = vi.fn();
  const text =
    '![许可图](resource://allowed)\n\n![外部图](https://example.test/image.png)\n\n![旧文件](file:///secret.png)\n\n<img src="https://example.test/html.png">';
  const view = render(
    <SourceText text={text} imageContext={context} onPreviewImage={preview} />,
  );
  expect(screen.getAllByRole("img")).toHaveLength(1);
  const original = screen.getByRole("img");
  fireEvent.error(original);
  view.rerender(
    <SourceText
      text={text}
      imageContext={{ conversationId: "second", traceId: "second-trace" }}
      onPreviewImage={preview}
    />,
  );
  const current = screen.getByRole("img", { name: "许可图" });
  expect(current.getAttribute("src")).toContain(
    "/conversations/second/turns/second-trace/resources?",
  );
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(screen.getByText("外部图：图片请在原回答或原件中查看")).toBeVisible();
});
