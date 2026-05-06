import { AxiosRequestConfig } from 'axios'
import { withAdminAuth } from './adminAuthConfig'
import { request } from './request'

export interface AdminMeResponse {
  user_id: number
  username: string
  role: string
  is_admin: boolean
  is_super_admin: boolean
}

export interface AdminOverviewResponse {
  admin_user: {
    id: number
    username: string
  }
  metrics: {
    users: number
    knowledge_bases: number
    documents: number
    sessions: number
    jobs: number
  }
  breakdown: {
    sessions_by_surface: Record<string, number>
    jobs_by_status: Record<string, number>
  }
  ops?: {
    runtime?: AdminRuntimeMetrics
    jobs?: AdminJobOpsMetrics
    deep_research?: Record<string, any>
  }
  phase2_reserved_modules: string[]
  uptime_secs: number
}

export interface AdminRuntimeMetrics {
  requests_total: number
  requests_4xx_total: number
  requests_5xx_total: number
  avg_latency_ms: number
  qps_avg: number
  qps_1m: number
  error_rate_5xx: number
  error_rate_1m: number
  window_seconds: number
}

export interface AdminJobOpsMetrics {
  queue_backlog: number
  pending_jobs: number
  running_jobs: number
  terminal_jobs: number
  success_rate?: number | null
  failure_rate?: number | null
}

export interface AdminUserItem {
  id: number
  username: string
  role: string
  is_admin: boolean
  is_super_admin: boolean
  is_active: boolean
}

export interface AdminUserListResponse {
  items: AdminUserItem[]
  total: number
  page: number
  page_size: number
}

export interface UpdateUserRolePayload {
  role: 'user' | 'admin' | 'super_admin'
}

export interface UpdateUserStatusPayload {
  is_active: boolean
  reason?: string
}

export interface AdminJobItem {
  id: number
  user_id: number
  knowledge_base_id: number
  type: string
  status: string
  progress: number
  total: number
  succeeded: number
  failed: number
  error?: string | null
  payload?: Record<string, any> | null
  created_at?: string | null
  updated_at?: string | null
}

export interface AdminJobListResponse {
  items: AdminJobItem[]
  total: number
  page: number
  page_size: number
}

export interface AdminAuditLogItem {
  id: number
  admin_user_id: number | null
  admin_username?: string | null
  action: string
  target_type: string
  target_id?: string | null
  detail_json: Record<string, any>
  created_at?: string | null
}

export interface AdminAuditLogListResponse {
  items: AdminAuditLogItem[]
  total: number
  page: number
  page_size: number
}

export interface AdminOpsMetricsResponse {
  runtime: AdminRuntimeMetrics
  jobs: AdminJobOpsMetrics
  deep_research?: Record<string, any>
  uptime_secs: number
}

export interface AdminDeepResearchRunItem {
  research_id: string
  status: string
  topic: string
  mode?: string
  user_id?: number
  submitted_at?: string
  started_at?: string
  finished_at?: string
  token_usage?: Record<string, any>
  error?: string
}

export interface AdminDeepResearchRunListResponse {
  items: AdminDeepResearchRunItem[]
  total: number
  page: number
  page_size: number
}

export interface AdminDeepResearchQueueItem {
  research_id: string
  topic: string
  status: string
  priority?: number
  effective_priority?: number
  wait_seconds?: number
  submitted_at?: string
  started_at?: string
  user_id?: number
}

export interface AdminDeepResearchQueueResponse {
  active_runs: number
  pending_runs: number
  max_active_runs: number
  active_items: AdminDeepResearchQueueItem[]
  pending_items: AdminDeepResearchQueueItem[]
}

export interface AdminDemoStatsItem {
  id: number
  visitor_id?: number
  ip: string
  path: string
  user_agent: string | null
  visited_at: string | null
}

export interface AdminDemoStatsByDay {
  day: string
  visits: number
  unique_ips: number
}

export interface AdminDemoStatsResponse {
  items: AdminDemoStatsItem[]
  total: number
  page: number
  page_size: number
  by_ip: { ip: string; count: number }[]
  by_day?: AdminDemoStatsByDay[]
  summary?: { unique_ips: number; today_visits: number }
}


export function adminLogin(
  payload: { username: string; password: string },
  options?: AxiosRequestConfig,
) {
  return request.post<{ access_token: string; token_type: string }>(
    'admin/auth/login',
    payload,
    options,
  )
}

export function getAdminMe(options?: AxiosRequestConfig) {
  return request.get<AdminMeResponse>('admin/me', withAdminAuth(options))
}

export function getAdminDemoStats(
  params?: {
    page?: number
    page_size?: number
    date_from?: string
    date_to?: string
  },
  options?: AxiosRequestConfig,
) {
  return request.get<AdminDemoStatsResponse>('admin/demo-stats', {
    ...withAdminAuth(options),
    params,
  })
}

export function getAdminOverview(options?: AxiosRequestConfig) {
  return request.get<AdminOverviewResponse>('admin/overview', withAdminAuth(options))
}

export function listAdminUsers(
  params?: {
    page?: number
    page_size?: number
    keyword?: string
    role?: string
  },
  options?: AxiosRequestConfig,
) {
  return request.get<AdminUserListResponse>('admin/users', {
    ...withAdminAuth(options),
    params,
  })
}

export function updateAdminUserRole(
  userId: number,
  payload: UpdateUserRolePayload,
  options?: AxiosRequestConfig,
) {
  return request.patch<{ user: AdminUserItem }>(
    `admin/users/${userId}/role`,
    payload,
    withAdminAuth(options),
  )
}

export function updateAdminUserStatus(
  userId: number,
  payload: UpdateUserStatusPayload,
  options?: AxiosRequestConfig,
) {
  return request.patch<{ user: AdminUserItem }>(
    `admin/users/${userId}/status`,
    payload,
    withAdminAuth(options),
  )
}

export function listAdminJobs(
  params?: {
    page?: number
    page_size?: number
    status?: string
    type?: string
    user_id?: number
  },
  options?: AxiosRequestConfig,
) {
  return request.get<AdminJobListResponse>('admin/jobs', {
    ...withAdminAuth(options),
    params,
  })
}

export function cancelAdminJob(
  jobId: number,
  payload?: { reason?: string },
  options?: AxiosRequestConfig,
) {
  return request.post<{ job: AdminJobItem }>(
    `admin/jobs/${jobId}/cancel`,
    payload || {},
    withAdminAuth(options),
  )
}

export function retryAdminJob(jobId: number, options?: AxiosRequestConfig) {
  return request.post<{
    source_job: AdminJobItem
    retry_job: AdminJobItem
  }>(`admin/jobs/${jobId}/retry`, {}, withAdminAuth(options))
}

export function listAdminAuditLogs(
  params?: {
    page?: number
    page_size?: number
    action?: string
    admin_user_id?: number
  },
  options?: AxiosRequestConfig,
) {
  return request.get<AdminAuditLogListResponse>('admin/audit-logs', {
    ...withAdminAuth(options),
    params,
  })
}

export function getAdminOpsMetrics(options?: AxiosRequestConfig) {
  return request.get<AdminOpsMetricsResponse>('admin/ops-metrics', withAdminAuth(options))
}

export function listAdminDeepResearchRuns(
  params?: {
    page?: number
    page_size?: number
    status?: string
    user_id?: number
  },
  options?: AxiosRequestConfig,
) {
  return request.get<AdminDeepResearchRunListResponse>('admin/deep-research/runs', {
    ...withAdminAuth(options),
    params,
  })
}

export function getAdminDeepResearchQueue(options?: AxiosRequestConfig) {
  return request.get<AdminDeepResearchQueueResponse>(
    'admin/deep-research/queue',
    withAdminAuth(options),
  )
}

export function cancelAdminDeepResearchRun(
  researchId: string,
  payload?: { reason?: string },
  options?: AxiosRequestConfig,
) {
  return request.post<{ research_id: string; status: string; message: string }>(
    `admin/deep-research/${encodeURIComponent(researchId)}/cancel`,
    payload || {},
    withAdminAuth(options),
  )
}

export function retryAdminDeepResearchRun(
  researchId: string,
  options?: AxiosRequestConfig,
) {
  return request.post<{
    source_research_id: string
    retry_research_id: string
    status: string
    queue_position?: number | null
    active_runs?: number
    pending_runs?: number
  }>(`admin/deep-research/${encodeURIComponent(researchId)}/retry`, {}, withAdminAuth(options))
}
