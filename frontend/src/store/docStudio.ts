import { proxy } from 'valtio'

export type DocStudioChatMessage = {
  id: string
  role: 'user' | 'agent' | 'system'
  content: string
  createdAt: number
  meta?: Record<string, any>
}

type FileBuffer = {
  content: string
  savedContent: string
  encoding: string
  dirty: boolean
  loading: boolean
}

const state = proxy({
  workspaceLoading: false,
  workspaces: [] as DocStudioAPI.WorkspaceSummary[],
  workspaceId: '' as string,
  workspaceConfig: {} as Record<string, any>,
  fileTree: [] as DocStudioAPI.FileNode[],
  files: {} as Record<string, FileBuffer>,
  activeFilePath: '' as string,
  openedFiles: [] as string[],
  chatMessages: [] as DocStudioChatMessage[],
  executionHistory: [] as DocStudioAPI.AgentStep[],
  compileResult: null as DocStudioAPI.CompileResult | null,
  agentStatus: {
    intentType: undefined as string | undefined,
    intentConfidence: undefined as number | undefined,
    plan: undefined as DocStudioAPI.AgentPlanStatus | undefined | null,
    warnings: [] as string[],
    traceId: undefined as string | undefined,
    operationId: undefined as string | undefined,
  },
})

const actions = {
  setWorkspaceLoading(value: boolean) {
    state.workspaceLoading = value
  },
  setWorkspaces(list: DocStudioAPI.WorkspaceSummary[]) {
    state.workspaces = list
  },
  setWorkspaceId(workspaceId: string) {
    if (state.workspaceId === workspaceId) return
    state.workspaceId = workspaceId
    state.workspaceConfig = {}
    state.fileTree = []
    state.files = {}
    state.activeFilePath = ''
    state.openedFiles = []
    state.chatMessages = []
    state.executionHistory = []
    state.compileResult = null
    state.agentStatus = {
      intentType: undefined,
      intentConfidence: undefined,
      plan: undefined,
      warnings: [],
      traceId: undefined,
      operationId: undefined,
    }
  },
  setWorkspaceConfig(config: Record<string, any>) {
    state.workspaceConfig = config
  },
  setFileTree(tree: DocStudioAPI.FileNode[]) {
    state.fileTree = tree
  },
  setActiveFile(path: string) {
    if (path === state.activeFilePath) return
    state.activeFilePath = path
    if (path && !state.openedFiles.includes(path)) {
      state.openedFiles.push(path)
    }
  },
  setOpenedFiles(paths: string[]) {
    state.openedFiles = paths
    if (!paths.includes(state.activeFilePath)) {
      state.activeFilePath = paths[paths.length - 1] || ''
    }
  },
  closeFile(path: string) {
    state.openedFiles = state.openedFiles.filter((item) => item !== path)
    if (state.activeFilePath === path) {
      state.activeFilePath = state.openedFiles[state.openedFiles.length - 1] || ''
    }
  },
  ensureFileBuffer(path: string) {
    if (!state.files[path]) {
      state.files[path] = {
        content: '',
        savedContent: '',
        encoding: 'utf-8',
        dirty: false,
        loading: false,
      }
    }
    return state.files[path]
  },
  setFileLoading(path: string, loading: boolean) {
    const buffer = actions.ensureFileBuffer(path)
    buffer.loading = loading
  },
  setFileContent(path: string, content: string, encoding = 'utf-8') {
    const buffer = actions.ensureFileBuffer(path)
    buffer.content = content
    buffer.savedContent = content
    buffer.encoding = encoding
    buffer.dirty = false
    buffer.loading = false
  },
  updateFileContent(path: string, content: string) {
    const buffer = actions.ensureFileBuffer(path)
    buffer.content = content
    buffer.dirty = content !== buffer.savedContent
  },
  markFileSaved(path: string, savedContent?: string) {
    const buffer = actions.ensureFileBuffer(path)
    buffer.savedContent = typeof savedContent === 'string' ? savedContent : buffer.content
    buffer.dirty = buffer.content !== buffer.savedContent
  },
  appendChatMessage(message: Omit<DocStudioChatMessage, 'id' | 'createdAt'> & { id?: string; createdAt?: number }) {
    const id = message.id || (crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`)
    const createdAt = message.createdAt ?? Date.now()
    state.chatMessages.push({
      id,
      createdAt,
      role: message.role,
      content: message.content,
      meta: message.meta,
    })
  },
  setChatMessages(messages: DocStudioChatMessage[]) {
    state.chatMessages = Array.isArray(messages) ? messages : []
  },
  setMessageFeedback(messageId: string, rating: DocStudioAPI.AgentFeedbackRating) {
    const target = state.chatMessages.find((msg) => msg.id === messageId)
    if (!target) return
    target.meta = {
      ...(target.meta || {}),
      feedback: rating,
    }
  },
  updateMessageMeta(messageId: string, patch: Record<string, any>) {
    const target = state.chatMessages.find((msg) => msg.id === messageId)
    if (!target) return
    target.meta = { ...(target.meta || {}), ...patch }
  },
  removeChatMessageById(messageId: string) {
    if (!messageId) return
    state.chatMessages = state.chatMessages.filter((msg) => msg.id !== messageId)
  },
  truncateMessagesFromIndex(startIndex: number) {
    if (!Number.isFinite(startIndex) || startIndex <= 0) {
      state.chatMessages = []
      return
    }
    state.chatMessages = state.chatMessages.slice(0, Math.floor(startIndex))
  },
  setExecutionHistory(history: DocStudioAPI.AgentStep[]) {
    state.executionHistory = history
  },
  setCompileResult(result: DocStudioAPI.CompileResult | null) {
    state.compileResult = result
  },
  setAgentStatus(payload: {
    intentType?: string | null
    intentConfidence?: number | null
    plan?: DocStudioAPI.AgentPlanStatus | null
    warnings?: string[] | null
    traceId?: string | null
    operationId?: string | null
  }) {
    state.agentStatus = {
      intentType: payload.intentType || undefined,
      intentConfidence: typeof payload.intentConfidence === 'number' ? payload.intentConfidence : undefined,
      plan: payload.plan ?? undefined,
      warnings: payload.warnings?.filter(Boolean) ?? [],
      traceId: payload.traceId || undefined,
      operationId: payload.operationId || undefined,
    }
  },
}

export const docStudioState = state
export const docStudioActions = actions
