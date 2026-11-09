import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { UserRolesWorkspace } from "./UserRolesWorkspace";
import type { ManagedUser } from "./libraryApi";

const MEBIBYTE = 1024 * 1024;

afterEach(() => vi.restoreAllMocks());

function managedUser(overrides: Partial<ManagedUser> = {}): ManagedUser {
  return {
    user_id: "1001",
    username: "staff",
    display_name: "测试同事",
    dept_id: null,
    role: "user",
    is_super_admin: false,
    editable_roles: ["user", "hr"],
    role_edit_note: null,
    personal_quota: { max_bytes: 250 * MEBIBYTE, max_files: 50 },
    can_edit_personal_quota: true,
    ...overrides,
  };
}

it("可独立禁止和恢复账号访问并立即更新界面", async () => {
  const request = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation((_, init) => {
      if (init?.method === "PUT" && typeof init.body === "string") {
        const { blocked } = JSON.parse(init.body) as {
          blocked: boolean;
        };
        return Promise.resolve(
          Response.json({
            access_blocked: blocked,
            access_block_reason: blocked ? "account" : null,
          }),
        );
      }
      return Promise.resolve(
        Response.json({ items: [managedUser({ can_edit_access: true })] }),
      );
    });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const user = userEvent.setup();
  render(<UserRolesWorkspace getCsrfToken={() => "csrf"} onBack={() => {}} />);
  const role = await screen.findByRole("combobox", { name: "测试同事的角色" });
  await user.selectOptions(role, "hr");
  await user.click(screen.getByRole("button", { name: "禁止访问" }));
  expect(await screen.findByText("已禁止访问")).toBeVisible();
  expect(role).toHaveValue("hr");
  await user.click(screen.getByRole("button", { name: "恢复访问" }));
  expect(await screen.findByText("可访问")).toBeVisible();
  expect(role).toHaveValue("hr");
  expect(
    request.mock.calls.filter(([, init]) => init?.method === "PUT"),
  ).toEqual([
    [
      expect.stringContaining("/api/ops/users/1001/access"),
      expect.objectContaining({ body: '{"blocked":true}' }),
    ],
    [
      expect.stringContaining("/api/ops/users/1001/access"),
      expect.objectContaining({ body: '{"blocked":false}' }),
    ],
  ]);
});

describe("个人资料额度管理", () => {
  it("普通管理员只能读额度，不能编辑他人个人额度", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({
        items: [managedUser({ can_edit_personal_quota: false })],
      }),
    );
    render(
      <UserRolesWorkspace
        getCsrfToken={() => "csrf"}
        onBack={() => undefined}
      />,
    );
    expect(await screen.findByText("250 MB · 50 份")).toBeVisible();
    expect(screen.queryByRole("spinbutton")).toBeNull();
    expect(screen.queryByRole("button", { name: "保存额度" })).toBeNull();
    expect(
      screen.getByRole("combobox", { name: "测试同事的角色" }),
    ).toBeVisible();
  });

  it("超管角色只读时仍可独立保存额度，结果立即回显", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((_input, init) => {
        if (init?.method === "PUT") {
          return Promise.resolve(
            Response.json({ max_bytes: 500 * MEBIBYTE, max_files: 80 }),
          );
        }
        return Promise.resolve(
          Response.json({
            items: [
              managedUser({
                role: "admin",
                is_super_admin: true,
                editable_roles: [],
                role_edit_note: "超级管理员身份不能修改。",
              }),
            ],
          }),
        );
      });
    const user = userEvent.setup();
    render(
      <UserRolesWorkspace
        getCsrfToken={() => "csrf"}
        onBack={() => undefined}
      />,
    );
    const form = await screen.findByRole("form", {
      name: "测试同事的个人资料额度",
    });
    expect(screen.getByText("超级管理员身份不能修改。")).toBeVisible();
    expect(screen.queryByRole("combobox")).toBeNull();
    const capacity = within(form).getByRole("spinbutton", {
      name: "测试同事的个人空间（MB）",
    });
    const files = within(form).getByRole("spinbutton", {
      name: "测试同事的个人资料份数",
    });
    await user.clear(capacity);
    await user.type(capacity, "500");
    await user.clear(files);
    await user.type(files, "80");
    await user.click(within(form).getByRole("button", { name: "保存额度" }));
    expect(
      await screen.findByText("已更新 测试同事 的个人资料额度。"),
    ).toBeVisible();
    expect(fetch).toHaveBeenCalledWith(
      "/api/ops/users/1001/personal-quota",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ max_bytes: 500 * MEBIBYTE, max_files: 80 }),
        headers: { "Content-Type": "application/json", "X-CSRF-Token": "csrf" },
      }),
    );
    expect(
      screen.getByRole("spinbutton", { name: "测试同事的个人空间（MB）" }),
    ).toHaveValue(500);
    expect(
      screen.getByRole("spinbutton", { name: "测试同事的个人资料份数" }),
    ).toHaveValue(80);
    expect(screen.getByRole("button", { name: "保存额度" })).toBeDisabled();
    expect(
      fetch.mock.calls.filter(([, init]) => init?.method === "PATCH"),
    ).toHaveLength(0);
    expect(fetch.mock.calls.filter(([, init]) => !init?.method)).toHaveLength(
      1,
    );
  });

  it("保存额度不保存或丢失尚未提交的角色选择", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((_input, init) =>
        Promise.resolve(
          Response.json(
            init?.method === "PUT"
              ? { max_bytes: 300 * MEBIBYTE, max_files: 50 }
              : { items: [managedUser()] },
          ),
        ),
      );
    const user = userEvent.setup();
    render(
      <UserRolesWorkspace
        getCsrfToken={() => "csrf"}
        onBack={() => undefined}
      />,
    );
    const role = await screen.findByRole("combobox", {
      name: "测试同事的角色",
    });
    await user.selectOptions(role, "hr");
    const capacity = screen.getByRole("spinbutton", {
      name: "测试同事的个人空间（MB）",
    });
    await user.clear(capacity);
    await user.type(capacity, "300");
    await user.click(screen.getByRole("button", { name: "保存额度" }));
    await screen.findByText("已更新 测试同事 的个人资料额度。");
    expect(role).toHaveValue("hr");
    expect(screen.getByRole("button", { name: "保存" })).toBeEnabled();
    expect(fetch.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(
      false,
    );
  });

  it("无效额度不请求后台，后台拒绝后保留输入供修正", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((_input, init) =>
        Promise.resolve(
          init?.method === "PUT"
            ? Response.json({ detail: "额度暂时无法保存。" }, { status: 503 })
            : Response.json({ items: [managedUser()] }),
        ),
      );
    const user = userEvent.setup();
    render(
      <UserRolesWorkspace
        getCsrfToken={() => "csrf"}
        onBack={() => undefined}
      />,
    );
    const capacity = await screen.findByRole("spinbutton", {
      name: "测试同事的个人空间（MB）",
    });
    await user.clear(capacity);
    await user.type(capacity, "0");
    await user.click(screen.getByRole("button", { name: "保存额度" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "容量和份数请填写大于0的整数。",
    );
    expect(fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(
      false,
    );
    await user.clear(capacity);
    await user.type(capacity, "500");
    await user.click(screen.getByRole("button", { name: "保存额度" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("额度暂时无法保存。"),
    );
    expect(capacity).toHaveValue(500);
    expect(screen.getByRole("button", { name: "保存额度" })).toBeEnabled();
    expect(screen.queryByText("已更新 测试同事 的个人资料额度。")).toBeNull();
  });
});
