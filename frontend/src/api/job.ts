import type { AxiosRequestConfig } from 'axios'
import { request } from './request'
import type { JobInfo } from './repository'

export function list(params?: { kbId?: number }, options?: AxiosRequestConfig) {
  const query = params?.kbId ? { kb_id: params.kbId } : undefined
  return request.get<JobInfo[]>('jobs/', {
    ...options,
    params: query,
  })
}

export function detail(jobId: number, options?: AxiosRequestConfig) {
  return request.get<JobInfo>(`jobs/${jobId}`, options)
}

