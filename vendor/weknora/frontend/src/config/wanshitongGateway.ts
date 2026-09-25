/** 湾事通管理端只使用同源网关和服务端管理员会话。 */
export const WANSHITONG_GATEWAY_AUTH = import.meta.env?.VITE_WANSHITONG_GATEWAY_AUTH === 'true'

export const WANSHITONG_ADMIN_BASE = '/kb/admin/'
export const WANSHITONG_ADMIN_LOGIN = '/kb/admin/ops/'
export const WANSHITONG_ENGINE_API = '/kb/api/engine-admin'
let adminCsrfToken = ''

/** 恢复现有 Console Session，CSRF 只保存在当前页面内存。 */
export async function resumeWanshitongAdminSession(): Promise<boolean> {
  if (!WANSHITONG_GATEWAY_AUTH) return true
  try {
    const response = await fetch('/kb/api/v1/console/session', {
      method: 'GET',
      credentials: 'include',
      cache: 'no-store',
    })
    if (!response.ok) return false
    const payload = await response.json() as { authenticated?: boolean; csrf_token?: string }
    if (payload.authenticated !== true || !payload.csrf_token) return false
    adminCsrfToken = payload.csrf_token
    return true
  } catch {
    return false
  }
}

export function gatewayCsrfHeaders(method: string): Record<string, string> {
  if (!WANSHITONG_GATEWAY_AUTH || /^(GET|HEAD|OPTIONS)$/i.test(method)) return {}
  return adminCsrfToken ? { 'X-CSRF-Token': adminCsrfToken } : {}
}

/** 原生 API 路径在白标构建中统一由服务端代理，不直连 WeKnora。 */
export function engineResourceUrl(path: string): string {
  if (!WANSHITONG_GATEWAY_AUTH) return path
  if (path.startsWith(WANSHITONG_ENGINE_API)) return path
  let resourcePath = path
  if (/^https?:\/\//i.test(path)) {
    try {
      const parsed = new URL(path)
      resourcePath = `${parsed.pathname}${parsed.search}${parsed.hash}`
    } catch {
      return path
    }
  }
  if (resourcePath.startsWith('/api/') || resourcePath === '/files' || resourcePath.startsWith('/files?')) {
    return `${WANSHITONG_ENGINE_API}${resourcePath}`
  }
  return path
}

export function redirectToWanshitongAdminLogin(): void {
  if (typeof window !== 'undefined' && WANSHITONG_GATEWAY_AUTH) {
    adminCsrfToken = ''
    window.location.replace(WANSHITONG_ADMIN_LOGIN)
  }
}
