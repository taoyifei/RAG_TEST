import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LibraryWorkspace } from "./LibraryWorkspace";
import { UserRolesWorkspace } from "./UserRolesWorkspace";

afterEach(() => vi.restoreAllMocks());

function requestPath(input: RequestInfo | URL): string {
  return typeof input === "string"
    ? input
    : input instanceof URL
      ? input.href
      : input.url;
}

describe("用户资料和角色", () => {
  it("按名称和准备状态查找资料，清除筛选后恢复完整列表和时间排序", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({
        scope: "personal",
        can_manage: true,
        items: [
          {
            id: "old",
            title: "采购指引.docx",
            status: "completed",
            created_at: "2026-09-01",
            size: 20,
            shared: false,
          },
          {
            id: "new",
            title: "报账附件.xlsx",
            status: "failed",
            created_at: "2026-11-01",
            size: 20,
            shared: false,
          },
          {
            id: "middle",
            title: "人力说明.pdf",
            status: "processing",
            created_at: "2026-10-01",
            size: 20,
            shared: false,
          },
        ],
      }),
    );
    const user = userEvent.setup();
    render(
      <LibraryWorkspace
        userRole="user"
        getCsrfToken={() => "csrf"}
        onBack={() => undefined}
      />,
    );
    await screen.findByText("采购指引.docx");
    const firstRow = () =>
      within(screen.getByRole("table")).getAllByRole("row")[1];
    expect(firstRow()).toHaveTextContent("报账附件.xlsx");
    await user.type(
      screen.getByRole("searchbox", { name: "搜索资料名称" }),
      "报账",
    );
    expect(screen.getByText("报账附件.xlsx")).toBeVisible();
    expect(screen.queryByText("采购指引.docx")).not.toBeInTheDocument();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "资料状态" }),
      "ready",
    );
    expect(
      screen.getByText("没有找到相关资料，换个名称或清除筛选试试。"),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "清除筛选" }));
    expect(screen.getByRole("searchbox", { name: "搜索资料名称" })).toHaveValue(
      "",
    );
    expect(firstRow()).toHaveTextContent("报账附件.xlsx");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "资料排序" }),
      "oldest",
    );
    expect(firstRow()).toHaveTextContent("采购指引.docx");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "资料状态" }),
      "parsing",
    );
    expect(screen.getByText("人力说明.pdf")).toBeVisible();
    expect(screen.queryByText("采购指引.docx")).not.toBeInTheDocument();
    expect(screen.getByText("找到 1 份资料，共 3 份")).toBeVisible();
  });

  it("切换个人和公共资料时重置筛选，不残留另一范围的结果", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const scope = new URL(
        requestPath(input),
        "http://localhost",
      ).searchParams.get("scope");
      return Promise.resolve(
        Response.json({
          scope,
          can_manage: scope === "personal",
          items: [
            {
              id: scope,
              title: scope === "personal" ? "个人记录.txt" : "公共制度.pdf",
              status: "completed",
              created_at: "2026-11-01",
              size: 20,
              shared: false,
            },
          ],
        }),
      );
    });
    const user = userEvent.setup();
    render(
      <LibraryWorkspace
        userRole="user"
        getCsrfToken={() => "csrf"}
        onBack={() => undefined}
      />,
    );
    await screen.findByText("个人记录.txt");
    await user.type(
      screen.getByRole("searchbox", { name: "搜索资料名称" }),
      "个人",
    );
    await user.click(screen.getByRole("button", { name: "公共资料" }));
    await screen.findByText("公共制度.pdf");
    expect(screen.getByRole("searchbox", { name: "搜索资料名称" })).toHaveValue(
      "",
    );
    expect(screen.queryByText("个人记录.txt")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "上传资料" }),
    ).not.toBeInTheDocument();
  });

  it("普通用户上传资料默认放入个人区，随后可以共享和取消共享", async () => {
    let uploaded = false;
    let shared = false;
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input, init) => {
        const path = requestPath(input);
        if (path.startsWith("/api/public/library?")) {
          return Promise.resolve(
            Response.json({
              scope: "personal",
              can_manage: true,
              items: uploaded
                ? [
                    {
                      id: "doc-1",
                      title: "个人笔记.txt",
                      status: "completed",
                      created_at: "2026-10-29T01:00:00Z",
                      size: 20,
                      shared,
                    },
                  ]
                : [],
            }),
          );
        }
        if (path === "/api/public/library" && init?.method === "POST") {
          expect((init.body as FormData).get("scope")).toBe("personal");
          expect((init.body as FormData).get("file")).toBeInstanceOf(File);
          expect(init.headers).toEqual({ "X-CSRF-Token": "csrf-token" });
          uploaded = true;
          return Promise.resolve(new Response(null, { status: 201 }));
        }
        if (path === "/api/public/library/doc-1/share") {
          expect(init?.headers).toEqual({ "X-CSRF-Token": "csrf-token" });
          shared = init?.method === "POST";
          return Promise.resolve(new Response(null, { status: 204 }));
        }
        throw new Error(`unexpected request: ${path}`);
      });
    const user = userEvent.setup();
    render(
      <LibraryWorkspace
        userRole="user"
        getCsrfToken={() => "csrf-token"}
        onBack={() => undefined}
      />,
    );
    await screen.findByText(/这里还没有资料/);
    expect(
      screen.queryByRole("button", { name: "人力内部资料" }),
    ).not.toBeInTheDocument();
    await user.upload(
      screen.getByLabelText("选择上传资料"),
      new File(["内部笔记"], "个人笔记.txt", { type: "text/plain" }),
    );
    await screen.findByText("个人笔记.txt");
    await user.click(screen.getByRole("button", { name: "共享给全员" }));
    await screen.findByRole("button", { name: "取消共享" });
    await user.click(screen.getByRole("button", { name: "取消共享" }));
    await screen.findByRole("button", { name: "共享给全员" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/public/library/doc-1/share",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("人力可以切换到独立内部区上传，公共资料浏览不显示上传按钮", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const scope = new URL(
        requestPath(input),
        "http://localhost",
      ).searchParams.get("scope");
      return Promise.resolve(
        Response.json({ scope, can_manage: scope !== "public", items: [] }),
      );
    });
    const user = userEvent.setup();
    render(
      <LibraryWorkspace
        userRole="hr"
        getCsrfToken={() => "csrf"}
        onBack={() => undefined}
      />,
    );
    await screen.findByRole("button", { name: "上传资料" });
    await user.click(screen.getByRole("button", { name: "公共资料" }));
    await screen.findByText(/资料发布后会显示在这里/);
    expect(
      screen.queryByRole("button", { name: "上传资料" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "人力内部资料" }));
    expect(
      await screen.findByRole("button", { name: "上传资料" }),
    ).toBeEnabled();
    expect(
      screen.getByText("仅人力角色和超级管理员可查看、检索和维护。"),
    ).toBeVisible();
  });

  it("普通管理员看不到人力内部区，超级管理员可以访问", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({ scope: "personal", can_manage: true, items: [] }),
    );
    const { rerender } = render(
      <LibraryWorkspace
        userRole="admin"
        getCsrfToken={() => "csrf"}
        onBack={() => undefined}
      />,
    );
    await screen.findByRole("button", { name: "上传资料" });
    expect(
      screen.queryByRole("button", { name: "人力内部资料" }),
    ).not.toBeInTheDocument();
    rerender(
      <LibraryWorkspace
        userRole="admin"
        isSuperAdmin
        getCsrfToken={() => "csrf"}
        onBack={() => undefined}
      />,
    );
    expect(screen.getByRole("button", { name: "人力内部资料" })).toBeVisible();
  });

  it.each([
    { label: "已登录用户", username: "staff", deptId: "42" },
    { label: "旧版历史用户", username: "1001", deptId: null },
  ])("$label 可按真实用户 ID 保存角色", async ({ username, deptId }) => {
    let role = "user";
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input, init) => {
        if (init?.method === "PATCH") {
          role = "hr";
          return Promise.resolve(new Response(null, { status: 204 }));
        }
        return Promise.resolve(
          Response.json({
            items: [
              {
                user_id: "1001",
                username,
                display_name: "测试同事",
                dept_id: deptId,
                role,
                is_super_admin: false,
                editable_roles: ["user", "hr"],
                role_edit_note: null,
              },
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
    await screen.findByText("测试同事");
    expect(screen.getByText("用户 ID：1001", { exact: false })).toBeVisible();
    if (deptId) {
      expect(screen.getByText("部门 ID：42")).toBeVisible();
    } else {
      expect(screen.getByText("登录账号待本人下次登录同步")).toBeVisible();
      expect(screen.getByText("登录系统暂未提供")).toBeVisible();
    }
    await user.selectOptions(
      screen.getByRole("combobox", { name: "测试同事的角色" }),
      "hr",
    );
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/ops/users/1001",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ role: "hr" }),
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": "csrf",
          },
        }),
      ),
    );
    expect(
      await screen.findByText("已将 测试同事 设置为人力角色。"),
    ).toBeVisible();
  });

  it("管理员同级和超管行只读，普通用户不提供管理员选项", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({
        items: [
          {
            user_id: "325",
            username: "taoyifei",
            display_name: "超级管理员同事",
            dept_id: "208",
            role: "admin",
            is_super_admin: true,
            editable_roles: [],
            role_edit_note: "超级管理员身份由服务端配置，不能在此修改。",
          },
          {
            user_id: "201",
            username: "peer",
            display_name: "另一位管理员",
            dept_id: "208",
            role: "admin",
            is_super_admin: false,
            editable_roles: [],
            role_edit_note: "仅超级管理员可以调整其他管理员的角色。",
          },
          {
            user_id: "138",
            username: "luoyang",
            display_name: "普通同事",
            dept_id: "208",
            role: "user",
            is_super_admin: false,
            editable_roles: ["user", "hr"],
            role_edit_note: null,
          },
        ],
      }),
    );
    render(
      <UserRolesWorkspace
        getCsrfToken={() => "csrf"}
        onBack={() => undefined}
      />,
    );
    await screen.findByText("超级管理员");
    expect(screen.getAllByRole("combobox")).toHaveLength(1);
    expect(
      screen.getByRole("combobox", { name: "普通同事的角色" }),
    ).toBeVisible();
    expect(screen.queryByRole("option", { name: "系统管理员" })).toBeNull();
    expect(
      screen.getByText("仅超级管理员可以调整其他管理员的角色。"),
    ).toBeVisible();
  });
});
