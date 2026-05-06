declare namespace API {
  type SessionSurface = 'deep_chat' | 'doc_studio'

  interface Session {
    created_at: string
    session_id: string
    session_name: string
    updated_at: string
    surface?: SessionSurface
    // user_id: string
  }

  interface SessionDefaults {
    retrievalStrategy: 'multi_stage' | 'graph' | 'multimodal_graph'
    rerankerStrategy: 'none' | 'supervised' | 'rl'
    topK: number
    language: 'zh' | 'en'
    streaming: boolean
    useSessionKnowledgeBase: boolean
    useUserKnowledgeBase: boolean
    userKnowledgeBaseId?: number | null
    llmProvider?: 'dashscope' | 'openai' | 'local'
    llmModel?: string | null
  }

  interface ChatUsage {
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
  }

  interface CreateSessionResponse {
    sessionId: string
    kbId?: number | null
    ephemeral: boolean
    defaults: SessionDefaults
    surface?: SessionSurface
  }

  interface SessionDetail {
    sessionId: string
    kbId?: number | null
    sessionName: string
    surface?: SessionSurface
  }

  interface DeepResearchCardState {
    status:
      | 'plan'
      | 'queued'
      | 'running'
      | 'completed'
      | 'failed'
      | 'cancelled'
    topic: string
    request: import('@/api/deepResearch').DeepResearchRequest
    plan?: import('@/api/deepResearch').DeepResearchPlan
    planError?: string
    planLoading?: boolean
    source?: 'composer' | 'suggestion'
    userMessage?: string
    researchId?: string
    queuePosition?: number | null
    activeRuns?: number | null
    pendingRuns?: number | null
    progress?: import('@/api/deepResearch').ProgressEvent[]
    toolCounts?: Record<string, number>
    blockStats?: {
      total?: number
      completed?: number
      pending?: number
      iteration?: number
      maxIterations?: number
      citations?: number
    }
    snapshotQueue?: import('@/api/deepResearch').DeepResearchTrace['queue']
    snapshotOutline?: import('@/api/deepResearch').DeepResearchPlan
    report?: import('@/api/deepResearch').DeepResearchReportPayload
    citations?: import('@/api/deepResearch').DeepResearchCitation[]
    statusMessage?: string
    lastStage?: string
    updatedAt?: string
  }

  interface ChatItem {
    id: number
    role: import('@/configs').ChatRole
    type: import('@/configs').ChatType
    loading?: boolean
    error?: string
    content?: string
    think?: string
    message_id?: string

    documents?: Document[]
    reference?: Reference[]
    recommended_questions?: string[]
    attachments?: ChatAttachment[]
    images?: ChatImageAttachment[]
    usage?: ChatUsage
    elapsed_seconds?: number
    deepResearch?: DeepResearchCardState
  }

  interface ChatAttachment {
    id: number
    title: string
    knowledgeBaseId?: number
  }

  interface ChatImageAttachment {
    id: string
    name: string
    dataUrl: string
    mimeType: string
    size: number
  }

  interface Document {
    document_id: string
    document_name: string
    content_with_weight: string
  }

  interface Reference {
    id?: string
    document_id?: string
    document_name?: string
    document_title?: string
    doi?: string
    content_with_weight?: string
    snippet?: string
    source_text?: string
    page?: number
    chunk_id?: string
    score?: number
    positions?: number[][]
    page_range?: number[]
    knowledge_base_id?: number
    structure_title?: string
    structure_path?: string
    structure_chunk_index?: number
    structure_chunk_total?: number
    element_type?: string
    logical_type?: string
    bbox_list?: number[][] | number[][][]
    offsets?: {
      start?: number
      end?: number
    }
    alignment_status?: string
    source?: string
    parser_engine?: string
  }
}
