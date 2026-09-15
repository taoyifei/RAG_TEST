import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { routes, useRouter } from "./router";

describe("类型化页面路由", () => {
  it("保留 URL 中的非敏感工作范围", () => {
    window.history.replaceState(
      {},
      "",
      "/admin?project=prj_1&knowledgeBase=kb_1",
    );
    const { result } = renderHook(() => useRouter());

    act(() => result.current.go(routes.modelServices));

    expect(result.current.path).toBe(routes.modelServices);
    expect(window.location.search).toContain("project=prj_1");
    expect(window.location.search).toContain("knowledgeBase=kb_1");
  });

  it("把现有页面的内部导航收敛到 admin 前缀", () => {
    window.history.replaceState({}, "", "/admin");
    const { result } = renderHook(() => useRouter());

    act(() => result.current.go("/jobs"));

    expect(result.current.path).toBe(routes.jobs);
    expect(window.location.pathname).toBe("/admin/jobs");
  });
});
