import { AxiosRequestConfig } from 'axios'
import { withAdminAuth } from './adminAuthConfig'
import { request } from './request'

export interface RetrievalVariant {
  tag: string
  text: string
  synthetic?: boolean
  language?: string
}

export interface RetrievalChunkPreview {
  chunk_id?: string
  score?: number
  document_id?: number
  page?: number
  source?: string
  element_type?: string
  logical_type?: string
  text_preview?: string
  metadata?: Record<string, any>
}

export interface RetrievalPathSample {
  path_id: string
  label: string
  query_tag: string
  source?: string
  hit_count: number
  hits: RetrievalChunkPreview[]
}

export interface PromptSectionDebug {
  role: string
  content: string
  length: number
}

export interface RetrievalPreviewPayload {
  kb_id: number
  query: string
  top_k?: number
  session_id?: string
  focus_doc_ids?: number[]
  boost_doc_ids?: number[]
  index_mode?: string
}

export interface RetrievalDebugResponse {
  kb_id: number
  query: string
  top_k: number
  variant_meta: Record<string, any>
  variants: RetrievalVariant[]
  index_plan: Array<Record<string, string | null | undefined>>
  index_mode?: string | null
  indices_used: string[]
  index_stats: Record<string, number>
  path_stats: Record<string, number>
  path_samples: RetrievalPathSample[]
  rrf_candidates: RetrievalChunkPreview[]
  rrf_candidates_count?: number  // RRF融合后的实际候选数
  mmr_chunks: RetrievalChunkPreview[]
  mmr_output_count?: number  // MMR输出的候选数（给精排的）
  rerank_top_k?: number  // 精排候选数（MMR输出数）
  rerank_candidates?: RetrievalChunkPreview[]  // 精排前的候选chunks
  rerank_scores?: number[]  // 精排后的分数列表
  rerank_enabled?: boolean  // 是否启用了精排
  final_chunks: RetrievalChunkPreview[]
  memory: Record<string, any>
  prompt_sections: PromptSectionDebug[]
  prompt_total_chars: number
  prompt_context_chars: number
}

export function getRetrievalPreview(
  payload: RetrievalPreviewPayload,
  options?: AxiosRequestConfig,
) {
  return request.post<RetrievalDebugResponse>(
    'admin/debug/retrieval-preview',
    payload,
    withAdminAuth(options),
  )
}

