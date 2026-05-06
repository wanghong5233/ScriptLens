/**
 * ScriptLens chat SSE shim
 *
 * doc-studio UI 用的是浏览器原生 EventSource：先 POST `/edit/async` 拿 run_id，
 * 再 `new EventSource(GET /edit/async/{run_id}/events?token=...)`。EventSource
 * 只能 GET、token 走 query string，无法直接对接 ScriptLens 的
 * `POST /api/scripts/{id}/chat (body) → SSE` 协议。
 *
 * 这里用 fetch + ReadableStream 实现一个 EventSource-shim：
 *   - 外接口完全兼容（addEventListener / close / onerror / onopen）
 *   - 内部解析 `event:` / `data:` / `id:` SSE 帧
 *   - 关闭时 AbortController 中断 fetch
 *
 * 与后端事件协议（agent_runtime progress_callback）完全一致：
 *   start / step / delta / status / runtime_model / plan
 *   tool_call_start / tool_call_end / interaction_required
 *   complete / error
 */

import { getApiBase } from './env'
import { userState } from '@/store/user'

// ============================================================
// 内部：runId → 启动时已写入的 chat 请求快照
// ============================================================

export interface ChatStreamArgs {
  scriptId: string
  question: string
  // doc-studio 把整段历史塞在 question 里（_format_history_into_intent 已合并），
  // 这里直接透传 question；history/role 暂用默认值。
  history?: Array<{ role: 'user' | 'assistant'; content: string }>
  role?: string
}

const pendingArgs = new Map<string, ChatStreamArgs>()

export function rememberChatArgs(runId: string, args: ChatStreamArgs): void {
  pendingArgs.set(runId, args)
}

// ============================================================
// EventSource shim
// ============================================================

type Listener = (event: MessageEvent) => void

interface SseFrame {
  event: string
  data: string
  id?: string
}

const READYSTATE_CONNECTING = 0
const READYSTATE_OPEN = 1
const READYSTATE_CLOSED = 2

export class ScriptLensAgentStream {
  /** 类似 EventSource.readyState */
  public readyState: 0 | 1 | 2 = READYSTATE_CONNECTING
  public onopen: ((event: Event) => void) | null = null
  public onerror: ((event: Event) => void) | null = null
  public onmessage: ((event: MessageEvent) => void) | null = null

  private listeners = new Map<string, Set<Listener>>()
  private abortController = new AbortController()

  constructor(private readonly runId: string) {
    void this.start()
  }

  addEventListener(type: string, listener: Listener): void {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, new Set())
    }
    this.listeners.get(type)!.add(listener)
  }

  removeEventListener(type: string, listener: Listener): void {
    this.listeners.get(type)?.delete(listener)
  }

  close(): void {
    if (this.readyState === READYSTATE_CLOSED) return
    this.readyState = READYSTATE_CLOSED
    this.abortController.abort()
    pendingArgs.delete(this.runId)
  }

  private dispatch(frame: SseFrame): void {
    const eventName = frame.event || 'message'
    const messageEvent = new MessageEvent(eventName, {
      data: frame.data,
      lastEventId: frame.id || '',
    })
    if (eventName === 'message' && this.onmessage) {
      this.onmessage(messageEvent)
    }
    const handlers = this.listeners.get(eventName)
    if (handlers) {
      for (const h of handlers) {
        try {
          h(messageEvent)
        } catch (err) {
          // listener 自身错误不影响其它 listener
          // eslint-disable-next-line no-console
          console.warn(`[SSE] listener for "${eventName}" threw:`, err)
        }
      }
    }
  }

  private dispatchError(reason: string): void {
    if (this.readyState === READYSTATE_CLOSED) return
    // eslint-disable-next-line no-console
    console.error(`[ScriptLensAgentStream] error: ${reason}`)
    if (this.onerror) {
      this.onerror(new Event('error'))
    }
    // 模拟 EventSource：error 后视为 closed
    this.close()
  }

  private async start(): Promise<void> {
    const args = pendingArgs.get(this.runId)
    if (!args) {
      this.dispatchError(`runId ${this.runId} 没有对应的 chat 请求快照`)
      return
    }

    const token = typeof userState.token === 'string' ? userState.token.trim() : ''
    if (!token) {
      this.dispatchError('未登录：缺少 access token')
      return
    }

    const url = `${getApiBase()}/scripts/${encodeURIComponent(args.scriptId)}/chat`
    const body = {
      question: args.question,
      history: args.history ?? [],
      role: args.role ?? 'general',
    }

    let response: Response
    try {
      response = await fetch(url, {
        method: 'POST',
        signal: this.abortController.signal,
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(body),
      })
    } catch (err: any) {
      if (err?.name === 'AbortError') return
      this.dispatchError(`fetch failed: ${err?.message || err}`)
      return
    }

    if (!response.ok) {
      // 让 doc-studio UI 上层的 onerror 看到非 2xx
      const text = await response.text().catch(() => '')
      this.dispatchError(`HTTP ${response.status}: ${text.slice(0, 200)}`)
      return
    }
    if (!response.body) {
      this.dispatchError('response.body is null（环境不支持 ReadableStream）')
      return
    }

    this.readyState = READYSTATE_OPEN
    if (this.onopen) {
      try {
        this.onopen(new Event('open'))
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn('[SSE] onopen threw:', err)
      }
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''

    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // SSE 帧用 "\n\n" 分隔
        let sep = buffer.indexOf('\n\n')
        while (sep !== -1) {
          const rawFrame = buffer.slice(0, sep)
          buffer = buffer.slice(sep + 2)
          const frame = parseSseFrame(rawFrame)
          if (frame) this.dispatch(frame)
          sep = buffer.indexOf('\n\n')
        }
      }
    } catch (err: any) {
      if (err?.name === 'AbortError') return
      this.dispatchError(`stream read failed: ${err?.message || err}`)
      return
    } finally {
      pendingArgs.delete(this.runId)
    }

    // 流自然结束（后端发完 complete + 关流）
    this.readyState = READYSTATE_CLOSED
  }
}

function parseSseFrame(raw: string): SseFrame | null {
  if (!raw.trim()) return null
  let event = 'message'
  const dataLines: string[] = []
  let id: string | undefined

  for (const lineRaw of raw.split('\n')) {
    const line = lineRaw.replace(/\r$/, '')
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    const valueRaw = colon === -1 ? '' : line.slice(colon + 1)
    const value = valueRaw.startsWith(' ') ? valueRaw.slice(1) : valueRaw

    switch (field) {
      case 'event':
        event = value
        break
      case 'data':
        dataLines.push(value)
        break
      case 'id':
        id = value
        break
      // retry 字段忽略（fetch 不支持自动重连）
    }
  }

  return { event, data: dataLines.join('\n'), id }
}

// ============================================================
// 公共入口（被 docStudio.ts 调用）
// ============================================================

export function openScriptLensAgentStream(runId: string): ScriptLensAgentStream {
  return new ScriptLensAgentStream(runId)
}
