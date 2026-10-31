import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { PublicChatComposer } from "./PublicChatComposer";
import type { PublicSearchScope } from "./publicApi";

function ScopeComposer() {
  const [scopes, setScopes] = useState<PublicSearchScope[]>(["public"]);
  return (
    <PublicChatComposer
      busy={false}
      onStop={vi.fn()}
      onSubmit={vi.fn()}
      searchScopes={scopes}
      onSearchScopesChange={setScopes}
      searchScopeOptions={[
        { id: "public", label: "公共知识库" },
        { id: "personal", label: "我的资料" },
      ]}
    />
  );
}

describe("湾事通提问输入框", () => {
  it("点击范围框外部收起，保留选择且不拦截外部操作", async () => {
    const user = userEvent.setup();
    const outsideAction = vi.fn();
    render(
      <>
        <ScopeComposer />
        <button onClick={outsideAction} type="button">
          其他操作
        </button>
      </>,
    );
    await user.click(
      screen.getByRole("button", { name: "资料范围：公共知识库" }),
    );
    await user.click(screen.getByRole("checkbox", { name: "我的资料" }));
    expect(
      screen.getByRole("group", { name: "选择问答资料范围" }),
    ).toBeVisible();

    await user.click(screen.getByRole("button", { name: "其他操作" }));
    expect(outsideAction).toHaveBeenCalledOnce();
    expect(
      screen.queryByRole("group", { name: "选择问答资料范围" }),
    ).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "资料范围：公共知识库 + 我的资料" }),
    );
    expect(screen.getByRole("checkbox", { name: "我的资料" })).toBeChecked();
    await user.click(screen.getByRole("textbox", { name: "向湾小度提问" }));
    expect(
      screen.queryByRole("group", { name: "选择问答资料范围" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "向湾小度提问" })).toHaveFocus();
  });

  it("Esc 收起范围框并把焦点还给入口按钮", async () => {
    const user = userEvent.setup();
    render(<ScopeComposer />);
    await user.click(
      screen.getByRole("button", { name: "资料范围：公共知识库" }),
    );
    await user.click(screen.getByRole("checkbox", { name: "我的资料" }));
    await user.keyboard("{Escape}");

    expect(
      screen.queryByRole("group", { name: "选择问答资料范围" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "资料范围：公共知识库 + 我的资料" }),
    ).toHaveFocus();
  });

  it("Tab 离开范围框会收起，不把焦点抢回入口", async () => {
    const user = userEvent.setup();
    render(
      <>
        <ScopeComposer />
        <button type="button">下一项操作</button>
      </>,
    );
    await user.click(
      screen.getByRole("button", { name: "资料范围：公共知识库" }),
    );
    await user.click(screen.getByRole("checkbox", { name: "我的资料" }));
    await user.tab();

    expect(
      screen.queryByRole("group", { name: "选择问答资料范围" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一项操作" })).toHaveFocus();
  });

  it("触屏点击空白处也可收起范围框", async () => {
    const user = userEvent.setup();
    render(<ScopeComposer />);
    await user.click(
      screen.getByRole("button", { name: "资料范围：公共知识库" }),
    );
    await user.pointer({ keys: "[TouchA]", target: document.body });

    expect(
      screen.queryByRole("group", { name: "选择问答资料范围" }),
    ).not.toBeInTheDocument();
  });

  it("默认公共知识库，可以多选或仅选个人资料，不能清空全部范围", async () => {
    const user = userEvent.setup();
    render(<ScopeComposer />);
    await user.click(
      screen.getByRole("button", { name: "资料范围：公共知识库" }),
    );
    const publicScope = screen.getByRole("checkbox", { name: "公共知识库" });
    const personal = screen.getByRole("checkbox", { name: "我的资料" });
    expect(publicScope).toBeChecked();
    expect(publicScope).toBeDisabled();
    expect(personal).not.toBeChecked();
    await user.click(personal);
    expect(
      screen.getByRole("button", { name: "资料范围：公共知识库 + 我的资料" }),
    ).toBeVisible();
    await user.click(publicScope);
    expect(
      screen.getByRole("button", { name: "资料范围：我的资料" }),
    ).toBeVisible();
    expect(personal).toBeChecked();
    expect(personal).toBeDisabled();
    expect(
      screen.queryByRole("checkbox", { name: "人力内部资料" }),
    ).not.toBeInTheDocument();
  });

  it("Shift+Enter 换行，Enter 发送完整多行问题", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    render(
      <PublicChatComposer busy={false} onStop={vi.fn()} onSubmit={onSubmit} />,
    );
    const textbox = screen.getByRole("textbox", { name: "向湾小度提问" });
    expect(screen.queryByText("当前 Demo 仅支持 DOCX")).not.toBeInTheDocument();

    await user.type(textbox, "第一行");
    await user.keyboard("{Shift>}{Enter}{/Shift}");
    await user.type(textbox, "第二行");
    expect(textbox).toHaveValue("第一行\n第二行");
    expect(onSubmit).not.toHaveBeenCalled();

    await user.keyboard("{Enter}");
    expect(onSubmit).toHaveBeenCalledOnce();
    expect(onSubmit).toHaveBeenCalledWith("第一行\n第二行");
  });
});
