import { AxiosRequestConfig } from 'axios'
import { withAdminAuth } from './adminAuthConfig'
import { getApiBase } from './env'
import { request } from './request'

export interface KnowledgeBase {
  id: number
  name: string
  description?: string | null
  rag_provider?: string | null
  rag_config?: Record<string, any> | null
  is_ephemeral: boolean
  created_at: string
  updated_at: string
}

export type DocumentProcessingStatus = 'pending' | 'parsing' | 'ready' | 'failed'

export interface RepositoryDocument {
  id: number
  knowledge_base_id: number
  title: string
  authors?: string[] | null
  abstract?: string | null
  publication_year?: number | null
  journal_or_conference?: string | null
  keywords?: string[] | null
  citation_count?: number | null
  fields_of_study?: string[] | null
  doi?: string | null
  semantic_scholar_id?: string | null
  source_url?: string | null
  local_pdf_path?: string | null
  file_hash?: string | null
  ingestion_source: string
  created_at: string
  updated_at: string
  structure_metadata?: Record<string, any> | null
  // Lifecycle fields populated by the document state machine.
  processing_status: DocumentProcessingStatus
  chunk_count: number
  failure_stage?: string | null
  failure_reason?: string | null
  last_processed_at?: string | null
}

export interface OnlineSearchParams {
  query: string
  limit?: number
  year?: string
  providers?: string[]
  rank_by?: string
}

export interface OnlineDocumentCandidate {
  title: string
  authors?: string[]
  abstract?: string | null
  publication_year?: number | null
  journal_or_conference?: string | null
  keywords?: string[] | null
  citation_count?: number | null
  fields_of_study?: string[] | null
  doi?: string | null
  semantic_scholar_id?: string | null
  source_url?: string | null
  ingestion_source: string
  highLight?: boolean | null
  quality_source?: string | null
  quality_rank?: string | null
  quality_label?: string | null
  quality_score?: number | null
  quality_labels?: Array<{ source: string; rank: string; label: string }> | null
}

export interface JobDetail {
  doc_id: number
  title: string
  status?: 'ok' | 'skipped_pdf' | 'failed'
  download_status?: 'downloaded' | 'skipped' | 'failed'
  parse_status?: 'parsed' | 'failed' | 'not_applicable'
  note?: string
  manual_download_url?: string
  local_pdf_path?: string | null
  error?: string
  parse_error?: string
  chunks?: number
}

export interface JobInfo {
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
  details?: JobDetail[]
  payload?: Record<string, any> | null
  created_at?: string
  updated_at?: string
}

export interface DocumentParseBlock {
  index: number
  text: string
  element_type?: string | null
  page?: number | null
  metadata: Record<string, any>
}

export interface DocumentParseStats {
  total_blocks: number
  nonempty_blocks: number
  total_chars: number
  element_types: Record<string, number>
  parser_engines: Record<string, number>
}

export interface DocumentParseStage {
  key: string
  title: string
  description?: string | null
  stats: DocumentParseStats
  blocks: DocumentParseBlock[]
}

export interface DocumentParsePreviewResponse {
  document_id: number
  knowledge_base_id: number
  filename?: string | null
  parser_order: string[]
  stages: DocumentParseStage[]
  stats: DocumentParseStats
  blocks: DocumentParseBlock[]
}

export function listKnowledgeBases(options?: AxiosRequestConfig) {
  return request.get<KnowledgeBase[]>('knowledgebases/', {
    ...options,
  })
}

export function createKnowledgeBase(
  payload: {
    name: string
    description?: string
    rag_provider?: string | null
    rag_config?: Record<string, any> | null
  },
  options?: AxiosRequestConfig,
) {
  return request.post<KnowledgeBase>('knowledgebases/', payload, {
    ...options,
  })
}

export function updateKnowledgeBase(
  params: {
    kbId: number
    payload: {
      name?: string
      description?: string
      rag_provider?: string | null
      rag_config?: Record<string, any> | null
    }
  },
  options?: AxiosRequestConfig,
) {
  const { kbId, payload } = params
  return request.patch<KnowledgeBase>(`knowledgebases/${kbId}`, payload, {
    ...options,
  })
}

export function deleteKnowledgeBase(kbId: number, options?: AxiosRequestConfig) {
  return request.delete<KnowledgeBase>(`knowledgebases/${kbId}`, {
    ...options,
  })
}

export function listDocuments(
  params: { kbId: number },
  options?: AxiosRequestConfig,
) {
  const { kbId, ...rest } = params
  return request.get<RepositoryDocument[]>(`knowledgebases/${kbId}/documents/`, {
    ...options,
    params: rest,
  })
}

export function upload(
  params: { kbId: number; file: File },
  options?: AxiosRequestConfig,
) {
  const { kbId, file } = params
  const form = new FormData()
  form.append('file', file)
  return request.post(`knowledgebases/${kbId}/documents/upload`, form, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
    ...options,
  })
}

export function remove(
  params: { kbId: number; docId: number },
  options?: AxiosRequestConfig,
) {
  const { kbId, docId, ...rest } = params
  return request.delete(`knowledgebases/${kbId}/documents/${docId}`, {
    ...options,
    params: rest,
  })
}

export function searchOnlineDocuments(
  params: { kbId: number; payload: OnlineSearchParams },
  options?: AxiosRequestConfig,
) {
  const { kbId, payload } = params
  return request.post<OnlineDocumentCandidate[]>(
    `knowledgebases/${kbId}/documents/ingest/search-online`,
    payload,
    options,
  )
}

export function addOnlineDocuments(
  params: { kbId: number; documents: OnlineDocumentCandidate[] },
  options?: AxiosRequestConfig,
) {
  const { kbId, documents } = params
  return request.post<JobInfo>(
    `knowledgebases/${kbId}/documents/ingest/add-online`,
    {
      documents,
    },
    options,
  )
}

export function parseIndexDocuments(
  params: { kbId: number; payload?: { doc_ids?: number[]; session_id?: string } },
  options?: AxiosRequestConfig,
) {
  const { kbId, payload } = params
  return request.post<JobInfo>(
    `knowledgebases/${kbId}/documents/parse-index`,
    payload ?? {},
    options,
  )
}

export function retryDocument(
  params: { kbId: number; docId: number },
  options?: AxiosRequestConfig,
) {
  const { kbId, docId } = params
  return request.post<JobInfo>(
    `knowledgebases/${kbId}/documents/${docId}/retry`,
    {},
    options,
  )
}

/**
 * 获取文档预览 URL
 * @param kbId 知识库 ID
 * @param docId 文档 ID
 * @param token JWT token（必须提供）
 * @returns 完整的预览 URL，包含 token 查询参数
 */
export function getDocumentPreviewUrl(kbId: number, docId: number, token: string): string {
  const baseURL = getApiBase()
  const url = `${baseURL}/knowledgebases/${kbId}/documents/${docId}/preview`
  if (token) {
    return `${url}?token=${encodeURIComponent(token)}`
  }
  return url
}

export function getDocumentParsePreview(
  params: { kbId: number; docId: number },
  options?: AxiosRequestConfig,
) {
  const { kbId, docId, ...rest } = params
  return request.get<DocumentParsePreviewResponse>(
    'admin/documents/parse-preview',
    withAdminAuth({
      ...options,
      params: {
        kb_id: kbId,
        doc_id: docId,
        ...rest,
      },
    }),
  )
}
