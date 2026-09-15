import { afterEach, describe, expect, it, vi } from "vitest";

import { detectProductMode } from "./product-mode";

afterEach(() => vi.restoreAllMocks());

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("产品模式探测", () => {
  it("仅在公共 Facade 明确 404 时进入 Universal", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ error: { code: "NOT_FOUND" } }, 404),
    );
    await expect(detectProductMode()).resolves.toBe("universal");
  });

  it("识别湾事通能力", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        mode: "wanshitong",
        stream: true,
        stream_protocol: "wanshitong-public-sse-v1",
        document_visibility: "all_internal",
        feedback: true,
      }),
    );
    await expect(detectProductMode()).resolves.toBe("wanshitong");
  });

  it("固定 Scope 损坏的业务错误仍进入湾事通管理员壳", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: "WANSHITONG_SCOPE_INVALID",
            message: "固定 Scope 不可用",
          },
        },
        409,
      ),
    );
    await expect(detectProductMode()).resolves.toBe("wanshitong");
  });

  it("网络故障不会静默回退到 Universal", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    await expect(detectProductMode()).rejects.toThrow("offline");
  });
});
