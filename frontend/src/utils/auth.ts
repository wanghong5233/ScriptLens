import type { NavigateFunction } from 'react-router-dom'

export function buildLoginPath(redirectPath?: string) {
  const fallback =
    typeof window === 'undefined'
      ? '/doc-studio'
      : `${window.location.pathname}${window.location.search || ''}`
  const target = redirectPath || fallback || '/doc-studio'
  const safeTarget = target.startsWith('/') && !target.startsWith('/admin') ? target : '/doc-studio'
  return `/login?redirect=${encodeURIComponent(safeTarget)}`
}

export function requireLogin(
  token: string | null | undefined,
  navigate: NavigateFunction,
  options?: {
    redirectPath?: string
    message?: string
  },
) {
  if (token) return true
  window.$app?.message?.info(options?.message || '请先登录后继续使用')
  navigate(buildLoginPath(options?.redirectPath))
  return false
}
