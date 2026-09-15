import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  routes,
  useRouter,
  useWanshitongRouter,
  wanshitongRoutes,
} from "./router";

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

  it("在 Universal 默认模式保留旧控制台直达路径", () => {
    window.history.replaceState({}, "", "/documents");
    const { result } = renderHook(() => useRouter());

    expect(result.current.path).toBe(routes.documents);
    expect(window.location.pathname).toBe("/documents");
  });
});

describe("湾事通管理员路由", () => {
  it("把旧文档路径收敛到固定 admin 路由", () => {
    window.history.replaceState({}, "", "/documents");
    const { result } = renderHook(() => useWanshitongRouter());

    expect(result.current.path).toBe(wanshitongRoutes.documents);
    expect(window.location.pathname).toBe("/admin/documents");
  });

  it("模型旧路径重定向到只读 models 页面", () => {
    window.history.replaceState({}, "", "/admin/model-services");
    const { result } = renderHook(() => useWanshitongRouter());

    expect(result.current.path).toBe(wanshitongRoutes.models);
    expect(window.location.pathname).toBe("/admin/models");
  });

  it("隐藏页面的直接路径返回概览且不产生空白页", () => {
    window.history.replaceState({}, "", "/admin/projects");
    const { result } = renderHook(() => useWanshitongRouter());

    expect(result.current.path).toBe(wanshitongRoutes.workspace);
    expect(window.location.pathname).toBe("/admin");
  });
});
