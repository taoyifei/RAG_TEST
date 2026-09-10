import { describe, expect, it } from "vitest";

import { localizeStatus, zhCN } from "./zh-CN";

describe("中文产品文案", () => {
  it("覆盖一级导航、状态和影响类型", () => {
    expect(Object.values(zhCN.navigation)).toEqual(
      expect.arrayContaining(["工作台", "知识库", "问答", "模型服务"]),
    );
    expect(localizeStatus("succeeded")).toBe("已完成");
    expect(localizeStatus("INSUFFICIENT_EVIDENCE")).toBe("暂无足够依据");
    expect(localizeStatus("PROVIDER_UNAVAILABLE")).toBe("依赖暂不可用");
    expect(localizeStatus("CONFIGURATION_REQUIRED")).toBe("需要配置模型能力");
    expect(localizeStatus("BUDGET_BLOCKED")).toBe("模型预算已阻断");
    expect(zhCN.impact.NEW_INDEX_REVISION_REQUIRED).toBe("需要构建新索引版本");
  });
});
