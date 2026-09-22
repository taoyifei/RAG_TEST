import { afterEach, describe, expect, it } from "vitest";

import {
  applicationPath,
  appBasePath,
  appHistoryLocation,
  currentReturnTo,
  withAppBase,
} from "./basePath";

afterEach(() => window.history.replaceState({}, "", "/"));

describe("应用外部前缀", () => {
  it("只剥离一次固定 kb 前缀", () => {
    expect(appBasePath("/kb/admin/history")).toBe("/kb");
    expect(applicationPath("/kb/admin/history")).toBe("/admin/history");
    expect(applicationPath("/kb/kb/admin")).toBe("/kb/admin");
    expect(applicationPath("/admin/history")).toBe("/admin/history");
  });

  it("集中给 API 和历史路由补前缀", () => {
    window.history.replaceState({}, "", "/kb/?draft=1#ignored");

    expect(withAppBase("/api/public/session")).toBe(
      "/kb/api/public/session",
    );
    expect(currentReturnTo()).toBe("/kb/?draft=1");
    expect(appHistoryLocation("/admin/jobs")).toBe(
      "/kb/admin/jobs?draft=1",
    );
  });

  it("根路径开发模式保持现有地址", () => {
    expect(withAppBase("/api/public/session")).toBe("/api/public/session");
  });
});
