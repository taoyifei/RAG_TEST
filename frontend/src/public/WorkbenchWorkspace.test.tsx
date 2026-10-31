import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkbenchWorkspace } from "./WorkbenchWorkspace";

const publicId = "11111111-1111-4111-8111-111111111111";
const personalId = "22222222-2222-4222-8222-222222222222";
const options = [
  { id: "public", label: "公共知识库" },
  { id: "personal", label: "我的资料" },
] as const;
const token = () => "employee-csrf";
function response(id = publicId, scope = "public") {
  return Response.json({
    workspace_id: id,
    search_scopes: [scope],
    scope_label: scope,
  });
}
afterEach(() => vi.restoreAllMocks());

describe("工作助手资料范围", () => {
  it("默认公共范围使用员工CSRF，普通角色没有人力范围", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(response());
    render(
      <WorkbenchWorkspace
        getCsrfToken={token}
        scopeOptions={options}
        theme="light"
      />,
    );
    const iframe = await screen.findByTitle("湾小度工作助手 · public");
    expect(iframe).toHaveAttribute("src", `/workbench/${publicId}/`);
    expect(screen.getByLabelText("公共知识库")).toBeChecked();
    expect(screen.getByLabelText("我的资料")).not.toBeChecked();
    expect(screen.queryByLabelText("人力内部资料")).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      "/api/public/workbench/bootstrap",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": "employee-csrf",
        },
        body: JSON.stringify({ search_scopes: ["public"] }),
      }),
    );
  });

  it("切换范围立即卸载旧iframe，旧请求晚到也不会混入当前工作区", async () => {
    let resolvePublic!: (response: Response) => void;
    let resolvePersonal!: (response: Response) => void;
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolvePublic = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolvePersonal = resolve;
          }),
      );
    const user = userEvent.setup();
    render(
      <WorkbenchWorkspace
        getCsrfToken={token}
        scopeOptions={options}
        theme="dark"
      />,
    );
    await user.click(screen.getByLabelText("公共知识库"));
    await user.click(screen.getByLabelText("我的资料"));
    await user.click(screen.getByRole("button", { name: "切换范围" }));
    expect(fetch).toHaveBeenCalledTimes(2);
    const oldSignal = (fetch.mock.calls[0][1] as RequestInit).signal;
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => {
      resolvePublic(response());
      await Promise.resolve();
    });
    expect(
      screen.queryByTitle("湾小度工作助手 · public"),
    ).not.toBeInTheDocument();
    await act(async () => {
      resolvePersonal(response(personalId, "personal"));
      await Promise.resolve();
    });
    expect(
      await screen.findByTitle("湾小度工作助手 · personal"),
    ).toHaveAttribute("src", `/workbench/${personalId}/`);
    let resolveNext!: (response: Response) => void;
    fetch.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveNext = resolve;
        }),
    );
    await user.click(screen.getByLabelText("公共知识库"));
    await user.click(screen.getByLabelText("我的资料"));
    await user.click(screen.getByRole("button", { name: "切换范围" }));
    expect(
      screen.queryByTitle("湾小度工作助手 · personal"),
    ).not.toBeInTheDocument();
    await act(async () => {
      resolveNext(response());
      await Promise.resolve();
    });
    expect(
      await screen.findByTitle("湾小度工作助手 · public"),
    ).toBeInTheDocument();
  });

  it("权限拒绝显示可操作的重试，未成功连接时不嵌入原生页面", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(null, { status: 403 }))
      .mockResolvedValueOnce(response());
    const user = userEvent.setup();
    render(
      <WorkbenchWorkspace
        getCsrfToken={token}
        scopeOptions={options}
        theme="dark"
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("当前账号无权");
    expect(document.querySelector("iframe")).toBeNull();
    await user.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByTitle("湾小度工作助手 · public");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("权限范围收回后立即隐藏旧iframe，主题切换不重建工作区", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(response());
    const { rerender } = render(
      <WorkbenchWorkspace
        getCsrfToken={token}
        scopeOptions={options}
        theme="light"
      />,
    );
    const frame = await screen.findByTitle("湾小度工作助手 · public");
    rerender(
      <WorkbenchWorkspace
        getCsrfToken={token}
        scopeOptions={options}
        theme="dark"
      />,
    );
    expect(screen.getByTitle("湾小度工作助手 · public")).toBe(frame);
    expect(fetch).toHaveBeenCalledTimes(1);
    rerender(
      <WorkbenchWorkspace
        getCsrfToken={token}
        scopeOptions={[options[1]]}
        theme="dark"
      />,
    );
    await waitFor(() => expect(document.querySelector("iframe")).toBeNull());
    expect(screen.getByRole("alert")).toHaveTextContent(
      "当前资料范围或登录状态已变化",
    );
  });
});
