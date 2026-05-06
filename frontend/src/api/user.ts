import { AxiosRequestConfig } from 'axios'
import { request } from './request'

export function register(
  params: {
    username: string
    password: string
  },
  options?: AxiosRequestConfig,
) {
  return request.post<{}>('users/register', params, options)
}

export function login(
  params: {
    username: string
    password: string
  },
  options?: AxiosRequestConfig,
) {
  return request.post<{
    access_token: string
  }>('users/login', params, options)
}

export function demoEntry(
  params?: {
    code?: string
  },
  options?: AxiosRequestConfig,
) {
  return request.post<{
    access_token: string
    token_type: string
    username: string
  }>('users/demo-entry', params ?? {}, options)
}

/** Demo 用户访问记录（仅 demo token 可调） */
export function postDemoVisit(params: { path: string }, options?: AxiosRequestConfig) {
  return request.post<{ ok: boolean }>('users/demo-visit', params, {
    ...options,
    loading: false,
    errorToast: false,
  })
}
