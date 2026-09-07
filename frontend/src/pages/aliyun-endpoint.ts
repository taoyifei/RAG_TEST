/** 只规范当前显式模式的可信主机，不推测 Workspace 或发出网络请求。 */
export function normalizeAliyunEndpoint(mode: string, input: string): string {
  if (mode !== "workspace_host" && mode !== "beijing_dashscope") {
    throw new Error("请选择端点模式。");
  }
  if (!input && mode === "beijing_dashscope") {
    return "https://dashscope.aliyuncs.com";
  }
  if (!input) throw new Error("请从北京业务空间复制 API Host。");
  const candidate = input.includes("://") ? input : `https://${input}`;
  const syntax = /^https:\/\/([a-z0-9.-]+)(?::443)?\/?$/i.exec(candidate);
  if (!syntax || syntax[0] !== candidate) {
    throw new Error("API Host 仅接受可信裸主机或无路径的 HTTPS 地址。");
  }
  const host = syntax[1].toLowerCase();
  const trusted =
    mode === "workspace_host"
      ? /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cn-beijing\.maas\.aliyuncs\.com$/.test(
          host,
        )
      : host === "dashscope.aliyuncs.com";
  if (!trusted) throw new Error("API Host 不在当前北京端点模式的可信范围。");
  return `https://${host}`;
}

export function previewAliyunEndpoint(mode: string, input: string) {
  try {
    return { endpoint: normalizeAliyunEndpoint(mode, input), error: undefined };
  } catch (error) {
    return { endpoint: undefined, error: (error as Error).message };
  }
}
