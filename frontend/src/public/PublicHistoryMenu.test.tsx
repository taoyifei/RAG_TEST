import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PublicChatComposer } from "./PublicChatComposer";
import { PublicHistoryMenu } from "./PublicHistoryMenu";

const sessions = [
  {
    conversation_id: "history-1",
    title: "报账材料怎么准备？",
    updated_at: "2026-10-31T10:00:00Z",
  },
];

describe("历史会话菜单", () => {
  it("选中历史后收起菜单，只打开一次选中的会话", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <PublicHistoryMenu
        busy={false}
        sessions={sessions}
        moreCount={2}
        onSelect={onSelect}
      />,
    );
    const trigger = screen.getByRole("button", { name: "历史会话" });
    await user.click(trigger);
    expect(
      screen.getByText("当前仅显示最近 50 个会话，另有 2 个较早会话。"),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: sessions[0].title }));

    expect(onSelect).toHaveBeenCalledExactlyOnceWith("history-1");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveFocus();
    expect(
      screen.queryByRole("button", { name: sessions[0].title }),
    ).not.toBeInTheDocument();
  });

  it("Esc 和外部点击均可关闭，外部按钮仍可一次生效", async () => {
    const user = userEvent.setup();
    const outsideAction = vi.fn();
    render(
      <>
        <PublicHistoryMenu
          busy={false}
          sessions={sessions}
          moreCount={0}
          onSelect={vi.fn()}
        />
        <button onClick={outsideAction} type="button">
          返回问答
        </button>
      </>,
    );
    const trigger = screen.getByRole("button", { name: "历史会话" });
    await user.click(trigger);
    await user.tab();
    expect(
      screen.getByRole("button", { name: sessions[0].title }),
    ).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveFocus();
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "返回问答" }));
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(outsideAction).toHaveBeenCalledOnce();
  });

  it("资料范围与历史会话互相切换，一次点击即可切换且不会叠加", async () => {
    const user = userEvent.setup();
    render(
      <>
        <PublicHistoryMenu
          busy={false}
          sessions={sessions}
          moreCount={0}
          onSelect={vi.fn()}
        />
        <PublicChatComposer
          busy={false}
          onStop={vi.fn()}
          onSubmit={vi.fn()}
          onSearchScopesChange={vi.fn()}
        />
      </>,
    );
    const history = screen.getByRole("button", { name: "历史会话" });
    const scopes = screen.getByRole("button", { name: "资料范围：公共知识库" });
    await user.click(history);
    await user.click(scopes);
    expect(history).toHaveAttribute("aria-expanded", "false");
    expect(scopes).toHaveAttribute("aria-expanded", "true");
    await user.click(history);
    expect(history).toHaveAttribute("aria-expanded", "true");
    expect(scopes).toHaveAttribute("aria-expanded", "false");
  });
});
