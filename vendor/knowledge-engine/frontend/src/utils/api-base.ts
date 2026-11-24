import { INDUSTRIAL_AGENT_ENGINE_API, INDUSTRIAL_AGENT_GATEWAY_AUTH } from '@/config/industrialAgentGateway'

export function getApiBaseUrl(): string {
  if (INDUSTRIAL_AGENT_GATEWAY_AUTH) return INDUSTRIAL_AGENT_ENGINE_API
  // LocalHub plugin patch (2026-04-29): respect vite's BASE_URL so that
  // axios calls work at `/app/knowledgeengine/` (LocalHub reverse proxy). Without
  // this · axios hits `/api/v1/...` at LocalHub root · gets 404 "Cannot
  // POST". Strip trailing slash so axios doesn't produce `/app/knowledgeengine//api/v1/...`.
  // See: plugins/knowledgeengine/patches/api-base-baseurl.patch
  const base = (import.meta.env.BASE_URL || '/').replace(/\/+$/, '');
  return base;
}
