export type PublicSessionEvent = "account-changed" | "logout";

export interface PublicSessionChannel {
  close: () => void;
  publish: (event: PublicSessionEvent) => void;
}

function channelName(deploymentId: string): string {
  return `wanshitong-session-${deploymentId}`;
}

/** 记录当前账号，并返回该部署是否发生了账号切换。 */
export function recordPublicIdentity(
  deploymentId: string,
  userId: string,
): boolean {
  const key = `${channelName(deploymentId)}:identity`;
  try {
    const previous = window.localStorage.getItem(key);
    window.localStorage.setItem(key, userId);
    return previous !== null && previous !== userId;
  } catch {
    return false;
  }
}

/** 创建带 deployment id 的多标签页通知通道。 */
export function openPublicSessionChannel(
  deploymentId: string,
  onEvent: (event: PublicSessionEvent) => void,
): PublicSessionChannel {
  const name = channelName(deploymentId);
  if (typeof BroadcastChannel !== "undefined") {
    const channel = new BroadcastChannel(name);
    channel.addEventListener("message", (message: MessageEvent<unknown>) => {
      if (message.data === "logout" || message.data === "account-changed") {
        onEvent(message.data);
      }
    });
    return {
      close: () => channel.close(),
      publish: (event) => channel.postMessage(event),
    };
  }

  const storageKey = `${name}:event`;
  const listener = (event: StorageEvent) => {
    if (event.key !== storageKey || !event.newValue) return;
    try {
      const payload = JSON.parse(event.newValue) as { event?: unknown };
      if (payload.event === "logout" || payload.event === "account-changed") {
        onEvent(payload.event);
      }
    } catch {
      // 其它页面写入的无效值不会改变登录态。
    }
  };
  window.addEventListener("storage", listener);
  return {
    close: () => window.removeEventListener("storage", listener),
    publish: (event) => {
      try {
        window.localStorage.setItem(
          storageKey,
          JSON.stringify({
            event,
            nonce:
              globalThis.crypto?.randomUUID?.() ??
              `${Date.now()}-${Math.random()}`,
          }),
        );
      } catch {
        // 存储不可用时当前标签页退出仍然有效。
      }
    },
  };
}
