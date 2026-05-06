import { AxiosHeaders, type AxiosRequestConfig } from 'axios'
import { adminAuthState } from '@/store/adminAuth'

export function withAdminAuth(options?: AxiosRequestConfig): AxiosRequestConfig {
  const token = adminAuthState.token
  const headers = AxiosHeaders.from((options?.headers ?? {}) as any)
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  return {
    ...options,
    headers,
  }
}

