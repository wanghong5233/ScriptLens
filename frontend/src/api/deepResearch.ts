import { AxiosRequestConfig } from 'axios'
import { userState } from '@/store/user'
import { getDeepResearchBase } from './env'
import { request } from './request'

const DEEP_RESEARCH_BASE = getDeepResearchBase()

function withDeepResearchConfig(config?: AxiosRequestConfig): AxiosRequestConfig {
  return {
    baseURL: DEEP_RESEARCH_BASE,
    ...config,
    headers: {
      ...(config?.headers ?? {}),
    },
  }
}

export type DeepResearchMode = 'queue' | 'tree'
export type DeepResearchStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface DeepResearchRequest {
  topic: string
  mode?: DeepResearchMode
  depth?: number
  breadth?: number
  max_parallel?: number
  max_iterations?: number
  iteration_mode?: 'fixed' | 'flexible'
  use_web_search?: boolean
  use_paper_search?: boolean
  use_code_exec?: boolean
  code_exec_snippets?: string[]
  top_k?: number
  index_mode?: string
  session_id?: string
  language?: string
  report_style?: string
  llm_provider?: 'dashscope' | 'openai'
  llm_model?: string
  metadata?: Record<string, any>
}

export interface IdeaGenerationRequest {
  topic?: string
  idea_count?: number
  session_id?: string
  language?: string
  constraints?: string[]
  notes?: IdeaGenerationNoteInput[]
  top_k?: number
  index_mode?: string
  metadata?: Record<string, any>
}

export interface IdeaGenerationNoteInput {
  title?: string
  content: string
  tags?: string[]
  source?: string
}

export interface NotebookNoteRequest {
  selection: string
  session_id?: string
  language?: string
  title?: string
  tags?: string[]
  top_k?: number
  index_mode?: string
  metadata?: Record<string, any>
}

export interface NotebookNoteResponse {
  note_markdown: string
  citations: DeepResearchCitation[]
  trace?: Record<string, any>
}

export interface DeepResearchCitation {
  citation_id: string
  ref_number?: number
  title?: string
  url?: string
  snippet?: string
  source_type?: string
  metadata?: Record<string, any>
}

export interface PlanItem {
  title: string
  question: string
  depth: number
  parent_title?: string | null
}

export interface DeepResearchPlan {
  items: PlanItem[]
}

export interface ToolTrace {
  tool_id: string
  citation_id: string
  tool_type: string
  query: string
  raw_answer: string
  summary: string
  timestamp: string
  raw_answer_truncated?: boolean
  raw_answer_original_size?: number
}

export interface DeepResearchRunSummary {
  blocks_total: number
  blocks_by_status: Record<string, number>
  citations_total: number
  tool_traces_total: number
  tool_traces_by_type: Record<string, number>
  decisions_total: number
  errors: Array<{
    block_id: string
    tool_id: string
    tool_type: string
    summary: string
    timestamp?: string
  }>
  generated_at?: string
}

export interface DeepResearchRunMeta {
  research_id: string
  status: DeepResearchStatus | string
  topic: string
  mode: DeepResearchMode
  priority?: number
  submitted_at?: string
  started_at?: string
  finished_at?: string
  resumed_at?: string
  resume_count?: number
  resume_requested_at?: string
  resume_pending?: boolean
  cancel_requested_at?: string
  last_progress_at?: string
  cancel_reason?: string
  duration_seconds?: number
  user_id?: number
  summary?: DeepResearchRunSummary
  context?: Record<string, any>
  token_usage?: Record<string, any>
  error?: string
  request?: Record<string, any>
}

export interface DeepResearchRunList {
  items: DeepResearchRunMeta[]
}

export interface DeepResearchSessionContextItem {
  research_id: string
  topic: string
  status: DeepResearchStatus | string
  submitted_at?: string
  finished_at?: string
  citations_total: number
  summary: string
}

export interface DeepResearchSessionContextResponse {
  session_id: string
  items: DeepResearchSessionContextItem[]
}

export interface DeepResearchArchive {
  research_id: string
  meta: DeepResearchRunMeta
  snapshot: DeepResearchSnapshot
  progress: ProgressEvent[]
  summary?: DeepResearchRunSummary
}

export interface DeepResearchBlockEvidence {
  research_id: string
  block_id: string
  block: TopicBlock
  notes: string[]
  citations: string[]
  citation_details: DeepResearchCitation[]
  tool_traces: ToolTrace[]
  decisions: Record<string, any>[]
  progress_events: ProgressEvent[]
}

export interface TopicBlock {
  block_id: string
  title: string
  question: string
  status: string
  depth: number
  parent_id?: string | null
  created_at: string
  updated_at: string
  iterations: number
  max_iterations: number
  followups_generated: boolean
  notes: string[]
  citations: string[]
  tool_traces: ToolTrace[]
  decisions?: Record<string, any>[]
  child_ids: string[]
}

export interface DeepResearchTrace {
  mode?: string
  queue?: {
    research_id: string
    max_length?: number | null
    block_counter?: number
    blocks?: TopicBlock[]
  }
  summary?: DeepResearchRunSummary
  plan?: DeepResearchPlan
  report_details?: DeepResearchReportDetails
  [key: string]: any
}

export interface DeepResearchReportDetails {
  outline?: string[]
  outline_detailed?: string[]
  notes?: string[]
  citation_table?: string[]
  draft_markdown?: string
  quality?: {
    paragraphs_total?: number
    paragraphs_with_citations?: number
    paragraphs_without_citations?: number
    citation_paragraph_coverage?: number | null
    citations_mentions?: number
    citations_distinct_count?: number
    citations_distinct?: number[]
    uncited_examples?: string[]
    sections?: Array<{
      title: string
      paragraphs_total?: number
      paragraphs_with_citations?: number
      citation_paragraph_coverage?: number | null
      citations_mentions?: number
    }>
    sections_without_citations?: string[]
  }
}

export interface DeepResearchReportPayload {
  research_id?: string
  status?: string
  report_markdown?: string
  report_markdown_truncated?: boolean
  report_markdown_full_chars?: number
  outline?: string[]
  notes?: string[]
  citation_table?: string[]
  draft_markdown?: string
  draft_markdown_truncated?: boolean
  draft_markdown_full_chars?: number
  snapshot_compact?: boolean
  summary?: DeepResearchRunSummary
  trace?: DeepResearchTrace
  report_details?: DeepResearchReportDetails
}

export interface DeepResearchResponse {
  research_id: string
  status: string
  report_markdown: string
  citations: DeepResearchCitation[]
  trace: DeepResearchTrace
}

export interface DeepResearchSubmitResponse {
  research_id: string
  status: DeepResearchStatus | string
  message?: string
  queue_position?: number
  active_runs?: number
  pending_runs?: number
}

export interface DeepResearchPriorityUpdateRequest {
  priority: number
}

export interface DeepResearchQueueItem {
  research_id: string
  topic: string
  status: DeepResearchStatus | string
  priority?: number
  effective_priority?: number
  wait_seconds?: number
  submitted_at?: string
  started_at?: string
  user_id?: number
}

export interface DeepResearchQueueStatus {
  active_runs: number
  pending_runs: number
  max_active_runs: number
  active_items: DeepResearchQueueItem[]
  pending_items: DeepResearchQueueItem[]
}

export type IdeaGenerationStatus = 'running' | 'completed' | 'failed'

export interface IdeaGenerationResponse {
  idea_id: string
  ideas_markdown: string
  citations: DeepResearchCitation[]
  ideas?: IdeaGenerationItem[]
  trace: Record<string, any>
}

export interface IdeaCandidate {
  title: string
  description?: string
  dimension?: string
  novelty?: string
  feasibility?: string
}

export interface IdeaGenerationItem {
  knowledge_point: string
  description: string
  research_ideas: IdeaCandidate[]
  kept_ideas: string[]
  rejected_ideas: string[]
  reasons: Record<string, string>
  statement_markdown?: string
}

export interface IdeaGenerationRunMeta {
  idea_id: string
  status: IdeaGenerationStatus | string
  topic: string
  started_at?: string
  finished_at?: string
  duration_seconds?: number
  user_id?: number
  error?: string
  request?: Record<string, any>
}

export interface IdeaGenerationRunList {
  items: IdeaGenerationRunMeta[]
}

export interface IdeaGenerationRunDetail {
  meta: IdeaGenerationRunMeta
  payload: IdeaGenerationResponse
}

export interface ProgressEvent {
  research_id: string
  stage: string
  event_type?: string
  message: string
  timestamp?: string
  payload?: Record<string, any>
}

export interface DeepResearchProgress {
  research_id: string
  items: ProgressEvent[]
  next_offset?: number
}

export interface DeepResearchSnapshot {
  research_id: string
  meta?: DeepResearchRunMeta
  outline?: DeepResearchPlan
  queue?: DeepResearchTrace['queue']
  citations?: Record<string, any>
  report?: DeepResearchReportPayload
}

export function runDeepResearch(
  payload: DeepResearchRequest,
  options?: AxiosRequestConfig,
) {
  return request.post<DeepResearchResponse>(
    '/deep-research',
    payload,
    withDeepResearchConfig(options),
  )
}

export function previewDeepResearchPlan(
  payload: DeepResearchRequest,
  options?: AxiosRequestConfig,
) {
  return request.post<DeepResearchPlan>(
    '/deep-research/plan',
    payload,
    withDeepResearchConfig(options),
  )
}

export function submitDeepResearch(
  payload: DeepResearchRequest,
  options?: AxiosRequestConfig,
) {
  return request.post<DeepResearchSubmitResponse>(
    '/deep-research/submit',
    payload,
    withDeepResearchConfig(options),
  )
}

export function replayDeepResearch(researchId: string, options?: AxiosRequestConfig) {
  return request.post<DeepResearchSubmitResponse>(
    `/deep-research/${encodeURIComponent(researchId)}/replay`,
    undefined,
    withDeepResearchConfig(options),
  )
}

export function cancelDeepResearch(researchId: string, options?: AxiosRequestConfig) {
  return request.post<DeepResearchSubmitResponse>(
    `/deep-research/${encodeURIComponent(researchId)}/cancel`,
    undefined,
    withDeepResearchConfig(options),
  )
}

export function resumeDeepResearch(researchId: string, options?: AxiosRequestConfig) {
  return request.post<DeepResearchSubmitResponse>(
    `/deep-research/${encodeURIComponent(researchId)}/resume`,
    undefined,
    withDeepResearchConfig(options),
  )
}

export function getDeepResearchProgress(researchId: string, options?: AxiosRequestConfig) {
  return request.get<DeepResearchProgress>(
    `/deep-research/${researchId}/progress`,
    withDeepResearchConfig(options),
  )
}

export function getDeepResearchProgressSince(
  researchId: string,
  offset: number,
  limit?: number,
  options?: AxiosRequestConfig,
) {
  return request.get<DeepResearchProgress>(
    `/deep-research/${encodeURIComponent(researchId)}/progress/since`,
    withDeepResearchConfig({
      ...options,
      params: {
        ...(options?.params ?? {}),
        offset,
        limit,
      },
    }),
  )
}

export function getDeepResearchSnapshot(
  researchId: string,
  options?: AxiosRequestConfig & { compact?: boolean },
) {
  const { compact, ...axiosOptions } = options ?? {}
  return request.get<DeepResearchSnapshot>(
    `/deep-research/${researchId}/snapshot`,
    withDeepResearchConfig({
      ...axiosOptions,
      params: {
        ...(axiosOptions.params ?? {}),
        compact: compact ? 'true' : undefined,
      },
    }),
  )
}

export function getDeepResearchArchive(researchId: string, options?: AxiosRequestConfig) {
  return request.get<DeepResearchArchive>(
    `/deep-research/${researchId}/archive`,
    withDeepResearchConfig(options),
  )
}

export function getDeepResearchBlockEvidence(
  researchId: string,
  blockId: string,
  options?: AxiosRequestConfig,
) {
  return request.get<DeepResearchBlockEvidence>(
    `/deep-research/${researchId}/blocks/${encodeURIComponent(blockId)}/evidence`,
    withDeepResearchConfig(options),
  )
}

export function runIdeaGeneration(payload: IdeaGenerationRequest, options?: AxiosRequestConfig) {
  return request.post<IdeaGenerationResponse>(
    '/idea-generation',
    payload,
    withDeepResearchConfig(options),
  )
}

export function generateNotebookNote(payload: NotebookNoteRequest, options?: AxiosRequestConfig) {
  return request.post<NotebookNoteResponse>('/notebook', payload, withDeepResearchConfig(options))
}

export function listIdeaGenerationRuns(options?: AxiosRequestConfig) {
  return request.get<IdeaGenerationRunList>(
    '/idea-generation/runs',
    withDeepResearchConfig(options),
  )
}

export function getIdeaGenerationRun(ideaId: string, options?: AxiosRequestConfig) {
  return request.get<IdeaGenerationRunDetail>(
    `/idea-generation/${encodeURIComponent(ideaId)}`,
    withDeepResearchConfig(options),
  )
}

export function getDeepResearchProgressStreamUrl(researchId: string) {
  const base = DEEP_RESEARCH_BASE.replace(/\/$/, '')
  const streamUrl = `${base}/deep-research/${encodeURIComponent(researchId)}/progress/stream`
  const token = typeof userState.token === 'string' ? userState.token.trim() : ''
  if (!token) return streamUrl
  const sep = streamUrl.includes('?') ? '&' : '?'
  return `${streamUrl}${sep}token=${encodeURIComponent(token)}`
}

export function getDeepResearchPlanStreamUrl() {
  const base = DEEP_RESEARCH_BASE.replace(/\/$/, '')
  return `${base}/deep-research/plan/stream`
}

export function getDeepResearchExportUrl(
  researchId: string,
  format: 'markdown' | 'html' | 'pdf' = 'markdown',
) {
  const base = DEEP_RESEARCH_BASE.replace(/\/$/, '')
  return `${base}/deep-research/${encodeURIComponent(researchId)}/export?format=${format}`
}

export function exportDeepResearchReport(
  researchId: string,
  format: 'markdown' | 'html' | 'pdf' = 'markdown',
  options?: AxiosRequestConfig,
) {
  return request.get<Blob>(
    `/deep-research/${encodeURIComponent(researchId)}/export`,
    withDeepResearchConfig({
      ...options,
      responseType: 'blob',
      params: {
        ...(options?.params ?? {}),
        format,
      },
    }),
  )
}

export function listDeepResearchRuns(options?: AxiosRequestConfig) {
  return request.get<DeepResearchRunList>('/deep-research/runs', withDeepResearchConfig(options))
}

export function listDeepResearchRunsBySession(
  sessionId: string,
  limit = 40,
  options?: AxiosRequestConfig,
) {
  return request.get<DeepResearchRunList>(
    `/deep-research/session/${encodeURIComponent(sessionId)}/runs`,
    withDeepResearchConfig({
      ...options,
      params: {
        ...(options?.params ?? {}),
        limit,
      },
    }),
  )
}

export function getDeepResearchSessionContext(
  sessionId: string,
  params?: { limit?: number; max_summary_chars?: number },
  options?: AxiosRequestConfig,
) {
  return request.get<DeepResearchSessionContextResponse>(
    `/deep-research/session/${encodeURIComponent(sessionId)}/context`,
    withDeepResearchConfig({
      ...options,
      params: {
        ...(options?.params ?? {}),
        ...(params ?? {}),
      },
    }),
  )
}

export function getDeepResearchQueueStatus(options?: AxiosRequestConfig) {
  return request.get<DeepResearchQueueStatus>(
    '/deep-research/queue',
    withDeepResearchConfig(options),
  )
}

export function updateDeepResearchPriority(
  researchId: string,
  payload: DeepResearchPriorityUpdateRequest,
  options?: AxiosRequestConfig,
) {
  return request.patch<DeepResearchSubmitResponse>(
    `/deep-research/${encodeURIComponent(researchId)}/priority`,
    payload,
    withDeepResearchConfig(options),
  )
}

export function getDeepResearchMeta(researchId: string, options?: AxiosRequestConfig) {
  return request.get<DeepResearchRunMeta>(
    `/deep-research/${encodeURIComponent(researchId)}`,
    withDeepResearchConfig(options),
  )
}
