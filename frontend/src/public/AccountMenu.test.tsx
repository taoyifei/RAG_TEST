import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { AccountMenu } from "./AccountMenu";

it("账号操作按需展开，Escape恢复焦点，外部点击不会被拦截", async () => {
  const user = userEvent.setup();
  const theme = vi.fn();
  const outside = vi.fn();
  render(
    <>
      <AccountMenu
        name="普通用户"
        theme="dark"
        onThemeChange={theme}
        onLogout={vi.fn()}
      />
      <button onClick={outside}>外部操作</button>
    </>,
  );
  expect(
    screen.queryByRole("button", { name: "退出湾小度" }),
  ).not.toBeInTheDocument();
  const trigger = screen.getByRole("button", { name: "账户与外观" });
  await user.click(trigger);
  await user.click(screen.getByRole("button", { name: "切换到浅色模式" }));
  expect(theme).toHaveBeenCalledOnce();
  await user.keyboard("{Escape}");
  expect(trigger).toHaveFocus();
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  await user.click(trigger);
  await user.click(screen.getByRole("button", { name: "外部操作" }));
  expect(outside).toHaveBeenCalledOnce();
  expect(trigger).toHaveAttribute("aria-expanded", "false");
});
