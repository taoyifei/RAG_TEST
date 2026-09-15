import {
  getPublicCapabilities,
  PublicApiError,
} from "../public/publicApi";

export type ProductMode = "universal" | "wanshitong";

/**
 * 只有服务端明确返回湾事通能力时才进入湾事通产品壳。
 *
 * Universal 部署没有公共 Facade，会明确返回 404。网络故障或服务端 5xx
 * 不能被当成 Universal，以免故障时意外暴露另一套页面。
 */
export async function detectProductMode(
  signal?: AbortSignal,
): Promise<ProductMode> {
  try {
    const capabilities = await getPublicCapabilities(signal);
    if (capabilities.mode !== "wanshitong") {
      throw new Error("服务端返回了未知产品模式。");
    }
    return "wanshitong";
  } catch (error) {
    if (error instanceof PublicApiError) {
      if (error.status === 404) return "universal";
      // 湾事通公共能力在固定 Scope 损坏时会明确返回业务错误；仍需进入
      // 管理员壳，才能登录后查看阻断原因并重新检查。
      return "wanshitong";
    }
    throw error;
  }
}
