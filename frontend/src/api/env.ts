/**
 * 统一 API 地址解析，保证本地开发与演示部署行为一致。
 * 演示部署时前端在 Vercel（demo 域名），API 在后端（api 域名），
 * 必须使用绝对地址，否则相对路径会打到 Vercel 导致 405 / 404。
 */

function trimTrailingSlash(s: string): string {
  return s.replace(/\/+$/, '')
}

function isLocalBackendBase(value: string): boolean {
  return /^https?:\/\/(localhost|127\.0\.0\.1):8000\/api\/?$/i.test(value)
}

/**
 * 主 API base（用于 request、getDocumentPreviewUrl 等）。
 * 生产环境必须通过 VITE_API_BASE 配置为绝对地址，否则会打到前端托管域名。
 */
export function getApiBase(): string {
  const v = (import.meta.env.VITE_API_BASE as string | undefined)?.trim()
  if (import.meta.env.DEV && (!v || isLocalBackendBase(v))) return '/api'
  return trimTrailingSlash(v || '/api')
}

/**
 * ScriptLens 没有独立 doc-studio 微服务（单服务架构），
 * 这里把 base 指向 /api/scripts，让原 doc-studio 客户端
 * 在能对齐的端点（list / detail / scenes / chat / feedback）上
 * 直接调 ScriptLens 后端；不能对齐的端点会 404，调试阶段再处理。
 */
export function getDocStudioBase(): string {
  const explicit = (import.meta.env.VITE_DOC_STUDIO_BASE as string | undefined)?.trim()
  if (explicit) return trimTrailingSlash(explicit)
  if (import.meta.env.DEV) return '/api/scripts'
  const apiBase = (import.meta.env.VITE_API_BASE as string | undefined)?.trim()
  if (apiBase && /^https?:\/\//i.test(apiBase)) {
    return `${trimTrailingSlash(apiBase)}/scripts`
  }
  return '/api/scripts'
}

/**
 * Deep Research API base。若未显式配置，则从 VITE_API_BASE 派生。
 */
export function getDeepResearchBase(): string {
  const explicit = (import.meta.env.VITE_DEEP_RESEARCH_BASE as string | undefined)?.trim()
  if (explicit) return trimTrailingSlash(explicit)
  if (import.meta.env.DEV) return '/api/deep-research'
  const apiBase = (import.meta.env.VITE_API_BASE as string | undefined)?.trim()
  if (apiBase && /^https?:\/\//i.test(apiBase)) {
    return `${trimTrailingSlash(apiBase)}/deep-research`
  }
  return '/api/deep-research'
}
