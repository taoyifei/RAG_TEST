const WANSHITONG_BASE_PATH = "/kb";

/** 返回当前页面实际使用的外部路径前缀；根路径保留本地开发兼容。 */
export function appBasePath(pathname = window.location.pathname): string {
  return pathname === WANSHITONG_BASE_PATH ||
    pathname.startsWith(`${WANSHITONG_BASE_PATH}/`)
    ? WANSHITONG_BASE_PATH
    : "";
}

/** 把浏览器路径规范为应用内部路由，只剥离一次 `/kb`。 */
export function applicationPath(pathname = window.location.pathname): string {
  const basePath = appBasePath(pathname);
  if (!basePath) return pathname || "/";
  return pathname.slice(basePath.length) || "/";
}

/** 为同源 API、路由或资源补上当前唯一外部前缀。 */
export function withAppBase(path: string): string {
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new Error(`应用路径必须是绝对同源路径：${path}`);
  }
  const basePath = appBasePath();
  return basePath && path !== basePath && !path.startsWith(`${basePath}/`)
    ? `${basePath}${path}`
    : path;
}

/** 返回包含外部前缀和查询参数、但不含 fragment 的当前页面地址。 */
export function currentReturnTo(): string {
  return `${window.location.pathname}${window.location.search}`;
}

/** 为 SPA 历史导航生成保留当前查询参数的外部路径。 */
export function appHistoryLocation(path: string): string {
  const url = new URL(window.location.href);
  url.pathname = withAppBase(path);
  return `${url.pathname}${url.search}`;
}

export { WANSHITONG_BASE_PATH };
