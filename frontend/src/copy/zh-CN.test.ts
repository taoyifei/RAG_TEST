import { describe, expect, it } from "vitest";

import { localizeStatus, zhCN } from "./zh-CN";

describe("中文产品文案", () => {
  it("覆盖一级导航、状态和影响类型", () => {
    expect(Object.values(zhCN.navigation)).toEqual(
      expect.arrayContaining(["工作台", "知识库", "问答", "模型服务"]),
    );
    expect(localizeStatus("succeeded")).toBe("已完成");
    expect(zhCN.impact.NEW_INDEX_REVISION_REQUIRED).toBe(
      "需要构建新索引版本",
    );
  });
});
