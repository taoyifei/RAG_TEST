import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LibraryWorkspace } from "./LibraryWorkspace";
import { UserRolesWorkspace } from "./UserRolesWorkspace";
import type { PersonalLibraryQuota } from "./libraryApi";

afterEach(() => vi.restoreAllMocks());

function requestPath(input: RequestInfo | URL): string {
  return typeof input === "string"
    ? input
    : input instanceof URL
      ? input.href
      : input.url;
}

const MEBIBYTE = 1024 * 1024;

function personalQuota(
  overrides: Partial<PersonalLibraryQuota> = {},
): PersonalLibraryQuota {
  return {
    max_bytes: 250 * MEBIBYTE,
    used_bytes: 0,
    max_files: 50,
    used_files: 0,
    max_file_bytes: 50 * MEBIBYTE,
    warning: false,
    full: false,
    active_parses: 0,
    queued_parses: 0,
    max_active_parses: 1,
    max_queued_parses: 5,
    upload_allowed: true,
    upload_block_reason: null,
    ...overrides,
  };
}

describe("用户资料和角色", () => {
  it("显示个人容量、份数及排队信息，80%提醒时仍可上传", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({
        scope: "personal",
        can_manage: true,
        can_share: false,
        items: [],
        quota: personalQuota({
          used_bytes: 200 * MEBIBYTE,
          used_files: 40,
          active_parses: 1,
          queued_parses: 2,
          warning: true,
        }),
      }),
    );
    render(
      <LibraryWorkspace
        userRole="user"
        getCsrfToken={() => "csrf"}
        onBack={() => undefined}
      />,
    );
    expect(await screen.findByText("200 MB / 250 MB")).toBeVisible();
    expect(screen.getByText("40 / 50 份")).toBeVisible();
    expect(
      screen.getByRole("progressbar", { name: "个人空间使用量" }),
    ).toHaveAttribute("value", String(200 * MEBIBYTE));
    expect(screen.getByText("正在解析 1 份 · 等待 2 / 5 份")).toBeVisible();
    expect(
      screen.getByText("个人空间或资料份数已使用80%以上，请留意剩余额度。"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "上传资料" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "共享给全员" })).toBeNull();
  });

  it("满额仅暂停上传，保留原件操作且删除后立即恢复额度", async () => {
    let removed = false;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((_input, init) => {
        if (init?.method === "DELETE") {
          removed = true;
          return Promise.resolve(Response.json({ success: true }));
        }
        return Promise.resolve(
          Response.json({
            scope: "personal",
            can_manage: true,
            can_share: false,
            quota: personalQuota({
              used_bytes: (removed ? 249 : 250) * MEBIBYTE,
              used_files: removed ? 49 : 50,
              full: !removed,
              warning: true,
              upload_allowed: removed,
              upload_block_reason: removed ? null : "个人资料已达50份上限。",
            }),
            items: removed
              ? []
              : [
                  {
                    id: "own",
                    title: "本人记录.txt",
                    status: "completed",
                    size: MEBIBYTE,
                    created_at: "2026-11-04",
                    shared: false,
                    can_manage: true,
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
    await screen.findByText("本人记录.txt");
    expect(screen.getByRole("button", { name: "上传资料" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下载" })).toBeEnabled();
    expect(screen.getByText("已有资料仍可问答、下载和删除。")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "删除" }));
    await screen.findByText("249 MB / 250 MB");
    expect(screen.getByText("49 / 50 份")).toBeVisible();
    expect(screen.getByRole("button", { name: "上传资料" })).toBeEnabled();
    expect(fetch).toHaveBeenCalledWith(
      "/api/public/library/own",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it.each([
    {
      label: "单文件超限",
      bytes: 50 * MEBIBYTE + 1,
      used: 0,
      message: "单个文件不能超过 50 MB。",
    },
    {
      label: "剩余空间不足",
      bytes: 2 * MEBIBYTE,
      used: 249 * MEBIBYTE,
      message:
        "个人空间不足，请先删除不再需要的资料，或联系超级管理员调整额度。",
    },
  ])("$label 时不发上传请求", async ({ bytes, used, message }) => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({
        scope: "personal",
        can_manage: true,
        items: [],
        quota: personalQuota({ used_bytes: used }),
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
    await screen.findByRole("button", { name: "上传资料" });
    const file = new File(["样本"], "说明.txt", { type: "text/plain" });
    Object.defineProperty(file, "size", { value: bytes });
    await user.upload(screen.getByLabelText("选择上传资料"), file);
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(
      false,
    );
  });

  it("解析排队已满时只停新增，失败资料仍可操作", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({
        scope: "personal",
        can_manage: true,
        quota: personalQuota({
          active_parses: 1,
          queued_parses: 5,
          upload_allowed: false,
          upload_block_reason: "已有5份资料等待解析，请稍后再上传。",
        }),
        items: [
          {
            id: "failed",
            title: "待处理.txt",
            status: "failed",
            size: 10,
            created_at: "2026-11-04",
            shared: false,
            can_manage: true,
          },
        ],
      }),
    );
    render(
      <LibraryWorkspace
        userRole="user"
        getCsrfToken={() => "csrf"}
        onBack={() => undefined}
      />,
    );
    expect(
      await screen.findByText("已有5份资料等待解析，请稍后再上传。"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "上传资料" })).toBeDisabled();
    for (const name of ["下载", "删除", "重试"]) {
      expect(screen.getByRole("button", { name })).toBeEnabled();
    }
  });

  it("后台拒绝并发超额后刷新用量并保留原因", async () => {
    let rejected = false;
    vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => {
      if (init?.method === "POST") {
        rejected = true;
        return Promise.resolve(
          Response.json(
            { detail: "其他上传已用满个人空间。" },
            { status: 413 },
          ),
        );
      }
      return Promise.resolve(
        Response.json({
          scope: "personal",
          can_manage: true,
          items: [],
          quota: personalQuota({
            used_bytes: rejected ? 250 * MEBIBYTE : 0,
            upload_allowed: !rejected,
            full: rejected,
            upload_block_reason: rejected ? "个人空间已满。" : null,
          }),
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
    await screen.findByRole("button", { name: "上传资料" });
    await user.upload(
      screen.getByLabelText("选择上传资料"),
      new File(["样本"], "说明.txt", { type: "text/plain" }),
    );
    expect(await screen.findByText("250 MB / 250 MB")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "其他上传已用满个人空间。",
    );
    expect(screen.getByRole("button", { name: "上传资料" })).toBeDisabled();
  });

  it.each([
    { scope: "public", label: "公共资料" },
    { scope: "hr_public", label: "公开人力制度" },
    { scope: "hr_internal", label: "人力内部资料" },
  ])("$label 不显示或应用个人额度", async ({ label }) => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const selected = new URL(
        requestPath(input),
        "http://localhost",
      ).searchParams.get("scope");
      return Promise.resolve(
        Response.json({
          scope: selected,
          can_manage: true,
          items: [],
          quota: personalQuota({ full: true, upload_allowed: false }),
        }),
      );
    });
    const user = userEvent.setup();
    render(
      <LibraryWorkspace
        userRole="admin"
        isSuperAdmin
        getCsrfToken={() => "csrf"}
        onBack={() => undefined}
      />,
    );
    await screen.findByRole("region", { name: "个人资料额度" });
    await user.click(screen.getByRole("button", { name: label }));
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "个人资料额度" })).toBeNull(),
    );
    expect(screen.getByRole("button", { name: "上传资料" })).toBeEnabled();
    expect(screen.getByRole("region", { name: label })).toBeVisible();
  });

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

  it.each([
    { label: "管理员", isSuperAdmin: false },
    { label: "超级管理员", isSuperAdmin: true },
  ])(
    "$label 上传自己的个人资料后可以共享和取消共享",
    async ({ isSuperAdmin }) => {
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
                can_share: true,
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
          userRole="admin"
          isSuperAdmin={isSuperAdmin}
          getCsrfToken={() => "csrf-token"}
          onBack={() => undefined}
        />,
      );
      await screen.findByText(/这里还没有资料/);
      await user.upload(
        screen.getByLabelText("选择上传资料"),
        new File(["内部笔记"], "个人笔记.txt", { type: "text/plain" }),
      );
      await screen.findByText("个人笔记.txt");
      await user.click(screen.getByRole("button", { name: "共享给全员" }));
      await screen.findByRole("button", { name: "取消共享" });
      expect(screen.getByText("已共享", { exact: true })).toBeVisible();
      await user.click(screen.getByRole("button", { name: "取消共享" }));
      await screen.findByRole("button", { name: "共享给全员" });
      expect(screen.queryByText("已共享", { exact: true })).toBeNull();
      expect(fetch).toHaveBeenCalledWith(
        "/api/public/library/doc-1/share",
        expect.objectContaining({ method: "DELETE" }),
      );
    },
  );

  it.each([
    {
      label: "普通员工",
      userRole: "user" as const,
      isSuperAdmin: false,
      canShare: undefined,
    },
    {
      label: "普通员工",
      userRole: "user" as const,
      isSuperAdmin: false,
      canShare: false,
    },
    {
      label: "人力角色",
      userRole: "hr" as const,
      isSuperAdmin: false,
      canShare: false,
    },
    {
      label: "管理员",
      userRole: "admin" as const,
      isSuperAdmin: false,
      canShare: false,
    },
    {
      label: "超级管理员",
      userRole: "admin" as const,
      isSuperAdmin: true,
      canShare: false,
    },
  ])(
    "$label 可以维护自己的个人资料，但共享关闭时不显示共享入口或状态（$canShare）",
    async ({ userRole, isSuperAdmin, canShare }) => {
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        Response.json({
          scope: "personal",
          can_manage: true,
          can_share: canShare,
          items: [
            {
              id: "private-ready",
              title: "个人笔记.txt",
              status: "completed",
              created_at: "2026-11-04",
              size: 20,
              shared: false,
              can_manage: true,
            },
            {
              id: "private-shared",
              title: "旧共享原件.txt",
              status: "failed",
              created_at: "2026-11-04",
              size: 20,
              shared: true,
              can_manage: true,
            },
          ],
        }),
      );
      render(
        <LibraryWorkspace
          userRole={userRole}
          isSuperAdmin={isSuperAdmin}
          getCsrfToken={() => "csrf"}
          onBack={() => undefined}
        />,
      );
      await screen.findByText("个人笔记.txt");
      expect(screen.getByRole("button", { name: "上传资料" })).toBeEnabled();
      expect(screen.getAllByRole("button", { name: "删除" })).toHaveLength(2);
      expect(screen.getByRole("button", { name: "重试" })).toBeEnabled();
      expect(screen.getAllByRole("button", { name: "下载" })).toHaveLength(2);
      expect(screen.queryByRole("button", { name: "共享给全员" })).toBeNull();
      expect(screen.queryByRole("button", { name: "取消共享" })).toBeNull();
      expect(screen.queryByText("已共享", { exact: true })).toBeNull();
      expect(
        screen.getByText(
          "仅本人可查看、检索、下载和维护；管理员和超级管理员也不能访问他人的个人资料。",
        ),
      ).toBeVisible();
    },
  );

  it.each([
    {
      label: "普通员工",
      userRole: "user" as const,
      isSuperAdmin: false,
      scope: "public",
      canManage: false,
    },
    {
      label: "人力角色",
      userRole: "hr" as const,
      isSuperAdmin: false,
      scope: "public",
      canManage: false,
    },
    {
      label: "管理员",
      userRole: "admin" as const,
      isSuperAdmin: false,
      scope: "public",
      canManage: true,
    },
    {
      label: "超级管理员",
      userRole: "admin" as const,
      isSuperAdmin: true,
      scope: "public",
      canManage: true,
    },
    {
      label: "普通员工",
      userRole: "user" as const,
      isSuperAdmin: false,
      scope: "hr_public",
      canManage: false,
    },
    {
      label: "人力角色",
      userRole: "hr" as const,
      isSuperAdmin: false,
      scope: "hr_public",
      canManage: true,
    },
    {
      label: "管理员",
      userRole: "admin" as const,
      isSuperAdmin: false,
      scope: "hr_public",
      canManage: true,
    },
    {
      label: "超级管理员",
      userRole: "admin" as const,
      isSuperAdmin: true,
      scope: "hr_public",
      canManage: true,
    },
    {
      label: "人力角色",
      userRole: "hr" as const,
      isSuperAdmin: false,
      scope: "hr_internal",
      canManage: true,
    },
    {
      label: "超级管理员",
      userRole: "admin" as const,
      isSuperAdmin: true,
      scope: "hr_internal",
      canManage: true,
    },
  ])(
    "$label 的 $scope 区域始终可下载，维护按钮跟随服务端权限（$canManage）",
    async ({ userRole, isSuperAdmin, scope, canManage }) => {
      vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
        const selectedScope = new URL(
          requestPath(input),
          "http://localhost",
        ).searchParams.get("scope");
        return Promise.resolve(
          Response.json({
            scope: selectedScope,
            can_manage: selectedScope === scope ? canManage : true,
            can_share: false,
            items:
              selectedScope === scope
                ? [
                    {
                      id: "policy",
                      title: "制度资料.pdf",
                      status: "failed",
                      created_at: "2026-11-04",
                      size: 20,
                      shared: false,
                      can_manage: canManage,
                    },
                  ]
                : [],
          }),
        );
      });
      const user = userEvent.setup();
      render(
        <LibraryWorkspace
          userRole={userRole}
          isSuperAdmin={isSuperAdmin}
          getCsrfToken={() => "csrf"}
          onBack={() => undefined}
        />,
      );
      const title =
        scope === "public"
          ? "公共资料"
          : scope === "hr_public"
            ? "公开人力制度"
            : "人力内部资料";
      await user.click(screen.getByRole("button", { name: title }));
      await screen.findByText("制度资料.pdf");
      expect(screen.getByRole("button", { name: "下载" })).toBeEnabled();
      for (const name of ["上传资料", "重试", "删除"]) {
        if (canManage) {
          expect(screen.getByRole("button", { name })).toBeEnabled();
        } else {
          expect(screen.queryByRole("button", { name })).toBeNull();
        }
      }
      expect(screen.queryByRole("button", { name: "共享给全员" })).toBeNull();
    },
  );

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
