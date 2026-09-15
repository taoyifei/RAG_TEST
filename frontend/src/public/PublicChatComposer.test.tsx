import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PublicChatComposer } from "./PublicChatComposer";

describe("湾事通提问输入框", () => {
  it("Shift+Enter 换行，Enter 发送完整多行问题", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    render(
      <PublicChatComposer busy={false} onStop={vi.fn()} onSubmit={onSubmit} />,
    );
    const textbox = screen.getByRole("textbox", { name: "向湾事通提问" });

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
