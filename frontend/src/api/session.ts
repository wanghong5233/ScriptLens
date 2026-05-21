import { AxiosRequestConfig } from 'axios'
import { request } from './request'
import {
  DEFAULT_PAGE_NUMBER,
  DEFAULT_PAGE_SIZE,
  DEFAULT_REPLAY_SINCE_SEQ,
} from '../constants/numbers'

export function list(
  params?: {
    surface?: API.SessionSurface
  },
  options?: AxiosRequestConfig,
) {
  return request.get<{
    sessions: API.Session[]
  }>('history/get_sessions', {
    ...options,
    params,
  })
}

export function detail(
  params: {
    session_id: string
  },
  options?: AxiosRequestConfig,
) {
  return request.get<
    {
      created_at: string
      message_id: string
      session_id: string
      user_question: string
      model_answer: string
      think?: string
      documents?: string
      recommended_questions?: string
      retrieval_content?: string
    }[]
  >('history/get_messages', {
    ...options,
    params,
  })
}

export function listMessages(
  params: {
    sessionId: string
    page?: number
    pageSize?: number
  },
  options?: AxiosRequestConfig,
) {
  const {
    sessionId,
    page = DEFAULT_PAGE_NUMBER,
    pageSize = DEFAULT_PAGE_SIZE,
  } = params
  return request.get<{
    total: number
    page: number
    pageSize: number
    items: {
      message_id: string
      session_id: string
      user_question: string
      model_answer: string
      create_time: string
      retrieval_content?: string
    }[]
  }>(`sessions/${sessionId}/messages`, {
    ...options,
    params: {
      page,
      page_size: pageSize,
    },
  })
}

export function info(
  params: { sessionId: string },
  options?: AxiosRequestConfig,
) {
  const { sessionId, ...rest } = params
  return request.get<API.SessionDetail>(`sessions/${sessionId}`, {
    ...options,
    params: rest,
  })
}

export function rename(
  params: { sessionId: string; sessionName: string },
  options?: AxiosRequestConfig,
) {
  const { sessionId, sessionName } = params
  return request.put<API.SessionDetail>(
    `sessions/${sessionId}/name`,
    {
      session_name: sessionName,
    },
    options,
  )
}

export function getDefaults(
  params: { sessionId: string },
  options?: AxiosRequestConfig,
) {
  const { sessionId, ...rest } = params
  return request.get<API.SessionDefaults>(`sessions/${sessionId}/defaults`, {
    ...options,
    params: rest,
  })
}

export function updateDefaults(
  params: { sessionId: string; defaults: API.SessionDefaults },
  options?: AxiosRequestConfig,
) {
  const { sessionId, defaults } = params
  return request.put<API.SessionDefaults>(
    `sessions/${sessionId}/defaults`,
    defaults,
    options,
  )
}

export function create(
  params: {
    kbId?: number
    ephemeral?: boolean
    defaults?: Partial<API.SessionDefaults>
    surface?: API.SessionSurface
  } = {
    ephemeral: true,
    surface: 'deep_chat',
  },
  options?: AxiosRequestConfig,
) {
  const payload = {
    ephemeral: params?.ephemeral ?? true,
    kbId: params?.kbId,
    defaults: params?.defaults,
    surface: params?.surface ?? 'deep_chat',
  }
  return request.post<API.CreateSessionResponse>('sessions/', payload, options)
}

export function chat(
  params: {
    id: string
    question: string
    stream?: boolean
    focusDocIds?: number[]
    topK?: number
    temperature?: number
    maxTokens?: number
    compressHistory?: boolean
    indexMode?: 'auto' | 'session_only' | 'global_only' | 'hybrid' | 'disabled'
    replaceFromMessageId?: string
    runId?: string
    llmProvider?: 'dashscope' | 'openai' | 'local'
    llmModel?: string
    imageAttachments?: {
      id?: string
      name: string
      dataUrl: string
      mimeType?: string
      size?: number
    }[]
  },
  options?: AxiosRequestConfig,
) {
  const { id, ...body } = params
  return request.post<ReadableStream>(
    `sessions/${id}/ask`,
    {
      stream: true,
      ...body,
    },
    {
      headers: {
        Accept: 'text/event-stream',
        'Content-Type': 'application/json',
      },
      responseType: 'stream',
      adapter: 'fetch',
      loading: false,
      ...options,
    },
  )
}

export function chatReplay(
  params: {
    id: string
    runId: string
    sinceSeq?: number
  },
  options?: AxiosRequestConfig,
) {
  const { id, runId, sinceSeq = DEFAULT_REPLAY_SINCE_SEQ } = params
  return request.get<ReadableStream>(
    `sessions/${id}/ask/replay/${runId}`,
    {
      headers: {
        Accept: 'text/event-stream',
        'Content-Type': 'application/json',
      },
      responseType: 'stream',
      adapter: 'fetch',
      loading: false,
      params: {
        since_seq: sinceSeq,
      },
      ...options,
    },
  )
}

export function chatCancel(
  params: {
    id: string
    runId: string
  },
  options?: AxiosRequestConfig,
) {
  const { id, runId } = params
  return request.post<{ run_id: string; cancelled: boolean }>(
    `sessions/${id}/ask/cancel/${runId}`,
    {},
    {
      loading: false,
      ...options,
    },
  )
}

export function rewind(
  params: {
    sessionId: string
    beforeMessageId?: string
    keepMessages?: number
  },
  options?: AxiosRequestConfig,
) {
  const { sessionId, beforeMessageId, keepMessages } = params
  return request.post<{
    deleted: boolean
    deleted_messages: number
    kept_messages: number
    before_message_id?: string
  }>(
    `sessions/${sessionId}/rewind`,
    {
      before_message_id: beforeMessageId,
      keep_messages: keepMessages,
    },
    options,
  )
}

export function upload(
  params: {
    sessionId: string
    file: File
  },
  options?: AxiosRequestConfig,
) {
  const { sessionId, file } = params
  const formData = new FormData()
  formData.append('file', file)
  return request.post(`sessions/${sessionId}/upload`, formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
    ...options,
  })
}

export function uploadForContext(
  params: {
    sessionId: string
    file: File
  },
  options?: AxiosRequestConfig,
) {
  const { sessionId, file } = params
  const formData = new FormData()
  formData.append('file', file)
  return request.post<{ filename: string; content: string }>(
    `sessions/${sessionId}/upload-for-context`,
    formData,
    {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
      ...options,
    },
  )
}

export function remove(
  params: { sessionId: string },
  options?: AxiosRequestConfig,
) {
  const { sessionId, ...rest } = params
  return request.delete<{ deleted: boolean; messages_deleted?: number }>(
    `sessions/${sessionId}`,
    {
      ...options,
      params: rest,
    },
  )
}
