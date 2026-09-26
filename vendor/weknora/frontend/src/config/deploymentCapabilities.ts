export const DEPLOYMENT_CAPABILITY_KEYS = [
  'organizations',
  'agents',
  'integrations.im',
  'integrations.embed',
  'integrations.api',
  'integrations.mcpserver',
  'settings.mcp',
  'settings.websearch',
  'settings.vectorstore',
  'settings.storage',
  'settings.sandbox',
  'settings.sandbox.docker',
  'settings.sandbox.host',
] as const

// 湾事通网关的入口约束不属于原生 /system/capabilities 返回值。
// 保留上面的原生键列表，与固定版 Go 声明一致。
export const WANSHITONG_CAPABILITY_KEYS = [
  'integrations.external',
  'settings.browserconnection',
  'settings.members',
  'settings.memory',
  'settings.ollama',
  'settings.systemadmin',
  'settings.weknoracloud',
  'toolbox',
] as const

export type DeploymentCapabilityKey =
  | typeof DEPLOYMENT_CAPABILITY_KEYS[number]
  | typeof WANSHITONG_CAPABILITY_KEYS[number]

export interface DeploymentCapability {
  supported: boolean
  reason?: string
}

export type DeploymentCapabilityMap = Partial<Record<DeploymentCapabilityKey, DeploymentCapability>>

// 8289 候选的有效入口取原生能力、网关白名单和部署依赖的交集。
// 这只控制界面；服务端的管理白名单仍负责最终授权。
const WANSHITONG_DISABLED_CAPABILITIES = new Set<DeploymentCapabilityKey>([
  'organizations',
  'agents',
  'integrations.im',
  'integrations.embed',
  'integrations.api',
  'integrations.mcpserver',
  'integrations.external',
  'settings.mcp',
  'settings.websearch',
  'settings.sandbox',
  'settings.sandbox.docker',
  'settings.sandbox.host',
  'settings.browserconnection',
  'settings.members',
  'settings.memory',
  'settings.ollama',
  'settings.systemadmin',
  'settings.weknoracloud',
  'toolbox',
])

/**
 * 原生部署的探测失败维持既有行为；湾事通候选的关闭项即使原生返回
 * supported=true 或探测失败，仍不显示会被网关拒绝的入口。
 */
export function isDeploymentCapabilitySupported(
  capabilities: DeploymentCapabilityMap,
  key?: DeploymentCapabilityKey,
  options?: { liteMode?: boolean; edition?: string; gatewayMode?: boolean },
): boolean {
  if (!key) return true
  if (options?.gatewayMode && WANSHITONG_DISABLED_CAPABILITIES.has(key)) return false
  if (key === 'organizations') {
    const isLite =
      options?.liteMode === true ||
      options?.edition?.trim().toLowerCase() === 'lite'
    if (isLite) return false
  }
  // Docker talks to a local Engine API (often docker.sock = host root), so
  // missing or failed capability probes must not leave the picker visible.
  // Host sandbox is Lite-desktop-only; keep the same fail-closed gate so a
  // missed probe does not show the new-session open-project UI on other deployments.
  if (key === 'settings.sandbox.docker' || key === 'settings.sandbox.host') {
    return capabilities[key]?.supported === true
  }
  return capabilities[key]?.supported !== false
}

export const SETTINGS_SECTION_CAPABILITY: Partial<Record<string, DeploymentCapabilityKey>> = {
  ollama: 'settings.ollama',
  weknoracloud: 'settings.weknoracloud',
  websearch: 'settings.websearch',
  vectorstore: 'settings.vectorstore',
  storage: 'settings.storage',
  members: 'settings.members',
  memory: 'settings.memory',
  mymemory: 'settings.memory',
  browserconnection: 'settings.browserconnection',
  'system-global': 'settings.systemadmin',
  'runtime-queues': 'settings.systemadmin',
  'platform-api-keys': 'settings.systemadmin',
  'system-audit-log': 'settings.systemadmin',
  sandbox: 'settings.sandbox',
  // Skills are baked into a sandbox image. Hide the catalog when the
  // deployment has no sandbox support, same as personal skill credentials.
  skills: 'settings.sandbox',
  // Skill credentials exist only because sandboxes do: the values are injected
  // into a skill script's process. A deployment without sandbox support has
  // nowhere to inject them, so the page would only ever show its empty state.
  envvars: 'settings.sandbox',
  mcp: 'settings.mcp',
}
