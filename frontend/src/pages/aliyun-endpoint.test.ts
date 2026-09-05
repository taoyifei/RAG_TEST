import { expect, it } from "vitest";

import { normalizeAliyunEndpoint } from "./aliyun-endpoint";

it.each([
  "api-synthetic.cn-beijing.maas.aliyuncs.com",
  "https://api-synthetic.cn-beijing.maas.aliyuncs.com",
  "https://api-synthetic.cn-beijing.maas.aliyuncs.com:443/",
])("合法等价主机输入 %s", (input) => {
  expect(normalizeAliyunEndpoint("workspace_host", input)).toBe(
    "https://api-synthetic.cn-beijing.maas.aliyuncs.com",
  );
});

it.each([
  "http://api-synthetic.cn-beijing.maas.aliyuncs.com",
  "https://user@api-synthetic.cn-beijing.maas.aliyuncs.com",
  "https://api-synthetic.cn-beijing.maas.aliyuncs.com:8443",
  "https://127.0.0.1",
  "https://10.0.0.1",
  "https://[::1]",
  "https://api-synthetic.cn-beijing.maas.aliyuncs.com/path",
  "https://api-synthetic.cn-beijing.maas.aliyuncs.com?",
  "https://api-synthetic.cn-beijing.maas.aliyuncs.com#fragment",
  "https://api-synthetic.cn-beijing.maas.aliyuncs.com%2f",
  "https://api-synthetic.cn-beijing.maas.aliyuncs.com\\evil",
  "https://api-synthetic.cn-beijing.maas.aliyuncs.com\r\n",
  "https://api-synthetic.cn-beijing.maas.aliyuncs.com\n",
  "https://api-synthetic.cn-shanghai.maas.aliyuncs.com",
  "https://api-synthetic.cn-beijing.maas.aliyuncs.com.evil.invalid",
  "https://ａpi-synthetic.cn-beijing.maas.aliyuncs.com",
  "dashscope.aliyuncs.com",
])("拒绝不可信或含多余组件输入 %s", (input) => {
  expect(() => normalizeAliyunEndpoint("workspace_host", input)).toThrow();
});

it("只使用显式模式，不自动按 Host 切换模式", () => {
  expect(() => normalizeAliyunEndpoint("", "dashscope.aliyuncs.com")).toThrow();
  expect(normalizeAliyunEndpoint("beijing_dashscope", "")).toBe(
    "https://dashscope.aliyuncs.com",
  );
});
