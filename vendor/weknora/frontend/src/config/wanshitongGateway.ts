/** 湾事通管理端只使用同源网关和服务端管理员会话。 */
export const WANSHITONG_GATEWAY_AUTH = import.meta.env?.VITE_WANSHITONG_GATEWAY_AUTH === 'true'

export const WANSHITONG_ADMIN_BASE = '/kb/admin/'
export const WANSHITONG_ADMIN_LOGIN = '/kb/admin/login'
export const WANSHITONG_ENGINE_API = '/kb/api/engine-admin'
let adminCsrfToken = ''

/** 只允许登录后返回本管理站已经注册的页面。 */
export function safeWanshitongReturnTo(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return '/overview'
  const path = value.split(/[?#]/, 1)[0]
  if (
    path === '/overview' ||
    path === '/public-settings' ||
    path === '/records' ||
    path === '/diagnostics' ||
    path === '/platform/knowledge-bases' ||
    /^\/platform\/knowledge-bases\/[^/]+$/.test(path) ||
    path === '/platform/settings'
  ) return value
  return '/overview'
}

/** 口令仅发送给既有 Console Session；原生 JWT 不进浏览器。 */
export async function loginWanshitongAdmin(bootstrapToken: string): Promise<void> {
  const response = await fetch('/kb/api/v1/console/session', {
    method: 'POST',
    credentials: 'include',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bootstrap_token: bootstrapToken }),
  })
  if (!response.ok) throw new Error(`登录失败（${response.status}）`)
  const payload = await response.json() as { csrf_token?: string }
  if (!payload.csrf_token) throw new Error('登录响应缺少会话校验信息')
  adminCsrfToken = payload.csrf_token
}

export async function logoutWanshitongAdmin(): Promise<void> {
  const response = await fetch('/kb/api/v1/console/session', {
    method: 'DELETE',
    credentials: 'include',
    cache: 'no-store',
    headers: gatewayCsrfHeaders('DELETE'),
  })
  if (!response.ok) throw new Error(`退出失败（${response.status}）`)
  adminCsrfToken = ''
}

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
    if (window.location.pathname === WANSHITONG_ADMIN_LOGIN) return
    const localPath = window.location.pathname.startsWith(WANSHITONG_ADMIN_BASE)
      ? window.location.pathname.slice(WANSHITONG_ADMIN_BASE.length - 1) + window.location.search
      : '/overview'
    const returnTo = safeWanshitongReturnTo(localPath)
    window.location.replace(`${WANSHITONG_ADMIN_LOGIN}?returnTo=${encodeURIComponent(returnTo)}`)
  }
}
