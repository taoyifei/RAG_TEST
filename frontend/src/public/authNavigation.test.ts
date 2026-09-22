import { afterEach, describe, expect, it } from "vitest";

import {
  consumeLoginDraft,
  saveLoginDraft,
  ssoEntryLocation,
} from "./authNavigation";

afterEach(() => {
  window.history.replaceState({}, "", "/");
  window.sessionStorage.clear();
});

describe("湾事通 SSO 导航", () => {
  it("生成同源 entry 并把完整 KB 页面作为 return_to", () => {
    window.history.replaceState({}, "", "/kb/admin/history?trace_id=1#x");

    expect(ssoEntryLocation()).toBe(
      "/kb/sso/entry?return_to=%2Fkb%2Fadmin%2Fhistory%3Ftrace_id%3D1",
    );
  });

  it("草稿只恢复一次且不会自动提交", () => {
    window.history.replaceState({}, "", "/kb/");
    saveLoginDraft("  登录前问题  ");

    expect(consumeLoginDraft()).toBe("登录前问题");
    expect(consumeLoginDraft()).toBe("");
  });
});
