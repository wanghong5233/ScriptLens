declare namespace API {
  type Result<T> = T & {
    status: 'success' | 'error'
    message: string
  }
}

declare namespace DocStudioAPI {
  interface WorkspaceSummary {
    workspaceId: string
    name: string
    mainFile?: string
    fileCount: number
    updatedAt: number
  }

  interface WorkspaceDetail extends WorkspaceSummary {
    config: Record<string, any>
  }

  interface KnowledgeBaseSummary {
    id: number
    name: string
    description?: string | null
    is_ephemeral?: boolean
    created_at?: string
    updated_at?: string
  }

  interface FileNode {
    name: string
    path: string
    type: 'file' | 'directory'
    size?: number
    modifiedAt?: number
    children?: FileNode[]
  }

  interface WorkspaceFilesResponse {
    workspaceId: string
    files: FileNode[]
    mainFile?: string
    config: Record<string, any>
  }

  interface FileContentResponse {
    path: string
    content: string
    encoding: string
  }

  interface SaveFileResponse {
    path: string
    size: number
    modified_at: number
    encoding: string
  }

  interface FileCreateResponse {
    path: string
    type: 'file' | 'directory'
  }

  interface UploadResponse {
    path: string
    size: number
  }

  interface AgentChange {
    file: string
    position?: {
      line?: number
      character?: number
    }
    type?: string
    content?: string
  }

  interface AgentStep {
    type: string
    content: string
    tool?: string
    parameters?: Record<string, any>
    result?: Record<string, any>
    timestamp?: number
  }

  interface FileDiff {
    file_path: string
    original_content: string
    modified_content: string
    is_truncated?: boolean
    added_lines?: number
    removed_lines?: number
  }

  interface AgentPlanStatus {
    steps: string[]
    completed_steps: number
    notes?: string | null
  }

  interface AgentResponse {
    success: boolean
    changes: AgentChange[]
    file_diffs?: FileDiff[]  // 完整的文件对比（用于 UI diff 预览）
    bibliography_updates?: Record<string, any>
    execution_history: AgentStep[]
    intent_type?: string
    intent_confidence?: number
    plan?: AgentPlanStatus | null
    warnings?: string[]
    trace_id?: string
    operation_id?: string
    history_path?: string
    episode_id?: string
  }

  interface OperationSummary {
    operation_id: string
    trace_id?: string
    workspace_id: string
    user_id: number
    timestamp: string
    success: boolean
    intent_type?: string | null
    user_intent: string
    modified_files?: string[]
    warnings?: string[]
    snapshot?: Record<string, any> | null
  }

  interface RevertOperationResponse {
    operation_id: string
    reverted_files: string[]
    deleted_files: string[]
    skipped_files: string[]
  }

  interface LlmProviderHealth {
    provider: string
    available: boolean
    in_cooldown: boolean
    cooldown_remaining_seconds?: number
    failures?: number
    last_error?: string | null
    last_success_at?: number | null
    last_failure_at?: number | null
  }

  interface LlmHealthSummary {
    preferred_provider?: string | null
    available_providers?: string[]
    providers: LlmProviderHealth[]
    fallback_enabled: boolean
    fallback_allow_explicit_provider: boolean
    failure_threshold: number
    cooldown_seconds: number
    request_timeout: number
    requested_by?: number
  }

  interface MetricsSummary {
    tools: Record<string, {
      success: number
      failure: number
      total: number
      avg_duration_seconds: number
    }>
    intents: Record<string, { low: number; medium: number; high: number }>
    plans: Record<string, { count: number; avg_tools: number; avg_duration_seconds: number }>
    llm?: Record<string, {
      success: number
      failure: number
      total: number
      avg_duration_seconds: number
      prompt_tokens: number
      completion_tokens: number
      total_tokens: number
      total_cost: number
    }>
    workspace_scans: { count: number; total_duration_seconds: number }
    workspace_cache_events: Record<string, number>
    feedback: Record<string, number>
  }

  interface CompileLog {
    command: string
    returncode: number
    log: string
  }

  interface CompileResult {
    success: boolean
    data?: {
      compiled: boolean
      compile_format?: 'latex' | 'markdown' | 'plaintext' | string
      target_path?: string
      preview_source?: string
      pdf_path?: string | null
      errors?: string[]
      warnings?: string[]
      logs?: CompileLog[]
    }
    error?: string | null
    summary?: string | null
  }

  interface CompileStatus {
    status?: string
    timestamp?: number
    result?: {
      success: boolean
      summary?: string | null
      data?: CompileResult['data']
      error?: string | null
    }
  }

  type AgentFeedbackRating = 'thumbs_up' | 'thumbs_down'
}
