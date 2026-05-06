import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert,
  Button,
  Dropdown,
  Empty,
  Form,
  Image,
  Input,
  Layout,
  message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Timeline,
  Tooltip,
  Tree,
  Typography,
} from 'antd'
import {
  FolderOpenOutlined,
  CopyOutlined,
  FileTextOutlined,
  ReloadOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  UploadOutlined,
  DeleteOutlined,
  FileAddOutlined,
  FolderAddOutlined,
  DownloadOutlined,
  SyncOutlined,
  EyeOutlined,
  EditOutlined,
  GlobalOutlined,
  ArrowUpOutlined,
  DatabaseOutlined,
  LikeOutlined,
  PictureOutlined,
  DislikeOutlined,
  HistoryOutlined,
  BarChartOutlined,
  EllipsisOutlined,
  CloseOutlined,
  MenuOutlined,
  MessageOutlined,
  MenuFoldOutlined,
  RollbackOutlined,
  SearchOutlined,
  ShareAltOutlined,
  CheckOutlined,
} from '@ant-design/icons'
import type { MenuProps } from 'antd'
import type { DataNode } from 'antd/es/tree'
import { useSnapshot } from 'valtio'
import Editor from '@monaco-editor/react'
import { AgentDiffReview, type AgentDiffReviewRef } from './AgentDiffReview'
import DocStudioWelcome from './component/doc-studio-welcome'
import { ChatMarkdown } from '@/components/markdown/ChatMarkdown'
import Recorder from '@/components/sender/recorder'
import { fetchLlmModels, type LlmModelCatalog } from '@/api/config'
import type React from 'react'
import type { TextAreaRef } from 'antd/es/input/TextArea'
import {
  compileWorkspace,
  createFileOrDirectory,
  createWorkspace,
  deleteFile,
  renameFileOrDirectory,
  openAsyncEventStream,
  fetchCompileStatus,
  fetchFileContent,
  fetchWorkspaceFiles,
  listWorkspaces,
  listOperations,
  fetchMetricsSummary,
  fetchLlmHealth,
  fetchOperationSnapshotFile,
  fetchAgentRunStatus,
  cancelAgentRun,
  respondAgentRunInteraction,
  rewindConversation,
  restoreCheckpoint,
  runAgentTask,
  runAgentTaskAsync,
  sendAgentFeedback,
  updateWorkspace,
  updateFileContent,
  revertOperation,
  uploadFile,
  downloadPdf,
  downloadFile,
  listAgentKnowledgeBases,
  bindWorkspaceSession,
  listWorkspaceMessages,
  getWorkspaceMessagesDebug,
  findSceneByRef,
  rewriteScript,
} from '@/api/docStudio'
import {
  create as createSession,
  list as listSessions,
  listMessages as listSessionMessages,
  rename as renameSession,
  remove as removeSession,
} from '@/api/session'
import { NOTEBOOK_LOCKED_PATHS, NOTEBOOK_WORKSPACE_ID } from '@/utils/notebook'
import { docStudioActions, docStudioState, type DocStudioChatMessage } from '@/store/docStudio'
import './index.scss'

const { Sider, Content, Header } = Layout
const { Text } = Typography

const findFirstFile = (nodes: DocStudioAPI.FileNode[]): string | undefined => {
  for (const node of nodes) {
    if (node.type === 'file') {
      return node.path
    }
    if (node.children?.length) {
      const child = findFirstFile(node.children)
      if (child) return child
    }
  }
  return undefined
}

const getErrorMessage = (error: any) => {
  if (!error) return '未知错误'
  return (
    error?.response?.data?.detail ||
    error?.response?.data?.message ||
    error?.message ||
    '请求失败'
  )
}

const isCanceledRequestError = (error: any) => {
  if (!error) return false
  const code = typeof error?.code === 'string' ? error.code.toUpperCase() : ''
  const name = typeof error?.name === 'string' ? error.name : ''
  const messageText = typeof error?.message === 'string' ? error.message.toLowerCase() : ''
  return (
    code === 'ERR_CANCELED' ||
    name === 'CanceledError' ||
    name === 'AbortError' ||
    messageText === 'canceled' ||
    messageText === 'cancelled' ||
    messageText.includes('取消重复请求')
  )
}

const showRequestError = (error: any, prefix = '') => {
  if (isCanceledRequestError(error)) return
  const detail = getErrorMessage(error)
  message.error(prefix ? `${prefix}${detail}` : detail)
}

const parseRetrievalContent = (value?: string) => {
  if (!value) return undefined
  try {
    return JSON.parse(value)
  } catch (error) {
    console.error('Failed to parse retrieval_content:', error)
    return undefined
  }
}

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

const copyTextToClipboard = async (text: string) => {
  if (!text) return
  // 优先使用现代 Clipboard API
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  // 兼容性降级：使用隐藏 textarea + execCommand
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  textarea.style.pointerEvents = 'none'
  textarea.style.left = '-9999px'
  document.body.appendChild(textarea)
  textarea.select()
  try {
    document.execCommand('copy')
  } finally {
    document.body.removeChild(textarea)
  }
}

const downloadTextAsFile = (text: string, fileName = 'output.txt') => {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = fileName
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

const resolveEditorLanguage = (filePath?: string) => {
  if (!filePath) return 'latex'
  const lower = filePath.toLowerCase()
  if (lower.endsWith('.md') || lower.endsWith('.markdown')) return 'markdown'
  if (lower.endsWith('.txt')) return 'plaintext'
  return 'latex'
}

const generateId = () =>
  window.crypto?.randomUUID?.() ?? `sel-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`

const readFileAsDataUrl = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = reader.result
      if (typeof result === 'string') {
        resolve(result)
      } else {
        reject(new Error('无法读取图片内容'))
      }
    }
    reader.onerror = () => reject(new Error('读取图片失败'))
    reader.readAsDataURL(file)
  })

const collectAllFilePaths = (nodes: DocStudioAPI.FileNode[]): string[] => {
  const result: string[] = []
  const walk = (items: DocStudioAPI.FileNode[]) => {
    for (const node of items) {
      if (node.type === 'file') {
        result.push(node.path)
      }
      if (node.children?.length) {
        walk(node.children)
      }
    }
  }
  walk(nodes)
  return result
}

const LIVE_TOOL_LABELS: Record<string, string> = {
  analyze_context_tool: '上下文分析',
  analyze_document_tool: '文档分析',
  semantic_code_search_tool: '语义检索',
  search_codebase_tool: '代码检索',
  read_file_range_tool: '按行读取',
  list_workspace_tree_tool: '浏览目录',
  create_directory_tool: '创建目录',
  create_file_tool: '创建文件',
  rename_move_path_tool: '重命名/移动',
  delete_path_tool: '删除路径',
  search_papers_tool: '论文检索',
  batch_search_papers_tool: '批量论文检索',
  insert_citation_tool: '插入引用',
  rewrite_selection_tool: '改写选区',
  rewrite_line_range_tool: '按行改写',
  update_bibliography_tool: '更新参考文献',
  insert_text_tool: '插入文本',
  compile_latex_tool: '编译 LaTeX',
  check_citation_consistency_tool: '检查引用一致性',
  check_bibliography_tool: '检查参考文献',
  web_search_tool: '网络搜索',
  reply_to_user_tool: '生成最终回复',
  answer_without_edit_tool: '直接回答',
}

const formatLiveToolName = (toolName?: string) => {
  const normalized = String(toolName || '').trim()
  if (!normalized) return ''
  if (LIVE_TOOL_LABELS[normalized]) return LIVE_TOOL_LABELS[normalized]
  return normalized.replace(/_tool$/, '').replace(/_/g, ' ')
}

const truncateLiveText = (value?: string, maxLength = 88) => {
  const text = String(value || '').trim()
  if (!text) return ''
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text
}

const buildSelectionPreview = (value: string, maxLength = SELECTION_PREVIEW_CHARS) => {
  const text = String(value || '').trim()
  if (text.length <= maxLength) return text
  const headLen = Math.max(120, Math.floor(maxLength * 0.62))
  const tailLen = Math.max(80, maxLength - headLen - 20)
  return `${text.slice(0, headLen).trimEnd()}\n...\n${text.slice(-tailLen).trimStart()}`
}

const normalizeLiveDeltaText = (value?: string) =>
  String(value || '')
    .replace(/\u0000/g, '')
    .replace(/\u200b/g, '')
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')

const chunkTextByLength = (value: string, chunkSize: number) => {
  const text = String(value || '')
  const size = Math.max(24, Number(chunkSize) || 64)
  const chunks: string[] = []
  for (let i = 0; i < text.length; i += size) {
    chunks.push(text.slice(i, i + size))
  }
  return chunks
}

const buildLivePreviewLines = (value?: string, maxLines = 6, chunkSize = 64) => {
  const normalized = normalizeLiveDeltaText(value)
  if (!normalized) return []
  const rawLines = normalized.split('\n').map((line) => line.replace(/\t/g, '  ').trimEnd())
  let lines = rawLines.filter((line) => line.trim().length > 0)
  if (lines.length === 0) {
    const compact = normalized.replace(/\s+/g, ' ').trim()
    if (!compact) return []
    lines = chunkTextByLength(compact, chunkSize)
  } else if (lines.length === 1 && lines[0].length > chunkSize * 2) {
    lines = chunkTextByLength(lines[0], chunkSize)
  }
  return lines.slice(-Math.max(1, maxLines))
}

type SelectionFragment = {
  id: string
  start: number
  end: number
  text: string
  filePath?: string
  placeholder: string
  startLine?: number
  endLine?: number
  startColumn?: number
  endColumn?: number
  totalChars?: number
  isRangeReference?: boolean
}

type FileMentionFragment = {
  id: string
  filePath: string
  placeholder: string
  strategy?: string
  totalChars?: number
  totalLines?: number
  fileHash?: string
  fileSize?: number
}

const SELECTION_PLACEHOLDER_REGEX = /@selection\d+/g
const FILE_PLACEHOLDER_REGEX = /@file\d+/g
const COMPOSER_PLACEHOLDER_REGEX = /@(selection|file)\d+/g
const containsSelectionPlaceholder = (value: string) =>
  new RegExp(SELECTION_PLACEHOLDER_REGEX.source).test(String(value || ''))
const containsFilePlaceholder = (value: string) =>
  new RegExp(FILE_PLACEHOLDER_REGEX.source).test(String(value || ''))

const normalizeSelectionPlaceholder = (value: unknown, fallbackIndex: number) => {
  const raw = String(value || '').trim()
  if (/^@selection\d+$/i.test(raw)) return raw
  return `@selection${fallbackIndex + 1}`
}

const normalizeSelectionFragments = (input: unknown): SelectionFragment[] => {
  const rawList = Array.isArray(input)
    ? input
    : input && typeof input === 'object'
      ? [input]
      : []
  return rawList
    .map((rawItem, idx) => {
      if (!rawItem || typeof rawItem !== 'object') return null
      const item = rawItem as Record<string, any>
      const text = String(item.text || '').trim()
      if (!text) return null
      const startRaw = Number(item.start)
      const endRaw = Number(item.end)
      const start = Number.isFinite(startRaw) ? Math.max(0, Math.floor(startRaw)) : 0
      const end = Number.isFinite(endRaw) ? Math.max(start, Math.floor(endRaw)) : Math.max(start, text.length)
      const startLineRaw = Number(item.startLine ?? item.start_line)
      const endLineRaw = Number(item.endLine ?? item.end_line)
      const startColumnRaw = Number(item.startColumn ?? item.start_column)
      const endColumnRaw = Number(item.endColumn ?? item.end_column)
      const totalCharsRaw = Number(item.totalChars ?? item.total_chars)
      const placeholder = normalizeSelectionPlaceholder(item.placeholder, idx)
      const filePathRaw = item.filePath ?? item.file_path
      const filePath = typeof filePathRaw === 'string' && filePathRaw.trim() ? filePathRaw.trim() : undefined
      const idRaw = typeof item.id === 'string' ? item.id.trim() : ''
      const id = idRaw || `${placeholder}-${start}-${end}-${idx}`
      return {
        id,
        start,
        end,
        text,
        filePath,
        placeholder,
        startLine: Number.isFinite(startLineRaw) ? Math.max(1, Math.floor(startLineRaw)) : undefined,
        endLine: Number.isFinite(endLineRaw) ? Math.max(1, Math.floor(endLineRaw)) : undefined,
        startColumn: Number.isFinite(startColumnRaw) ? Math.max(1, Math.floor(startColumnRaw)) : undefined,
        endColumn: Number.isFinite(endColumnRaw) ? Math.max(1, Math.floor(endColumnRaw)) : undefined,
        totalChars: Number.isFinite(totalCharsRaw) ? Math.max(0, Math.floor(totalCharsRaw)) : text.length,
        isRangeReference: Boolean(item.isRangeReference ?? item.is_range_reference),
      } as SelectionFragment
    })
    .filter((item): item is SelectionFragment => Boolean(item))
}

const normalizeFileMentionPlaceholder = (value: unknown, fallbackIndex: number) => {
  const raw = String(value || '').trim()
  if (/^@file\d+$/i.test(raw)) return raw
  return `@file${fallbackIndex + 1}`
}

const normalizeFileMentionFragments = (input: unknown): FileMentionFragment[] => {
  const rawList = Array.isArray(input)
    ? input
    : input && typeof input === 'object'
      ? [input]
      : []
  return rawList
    .map((rawItem, idx) => {
      if (!rawItem || typeof rawItem !== 'object') return null
      const item = rawItem as Record<string, any>
      const filePathRaw = item.filePath ?? item.file_path ?? item.path
      const filePath = typeof filePathRaw === 'string' ? filePathRaw.trim() : ''
      if (!filePath) return null
      const placeholder = normalizeFileMentionPlaceholder(item.placeholder, idx)
      const idRaw = typeof item.id === 'string' ? item.id.trim() : ''
      const strategyRaw = typeof item.strategy === 'string' ? item.strategy.trim() : ''
      const totalCharsRaw = Number(item.totalChars ?? item.total_chars)
      const totalLinesRaw = Number(item.totalLines ?? item.total_lines)
      const fileHashRaw = String(item.fileHash ?? item.file_hash ?? item.hash ?? '').trim().toLowerCase()
      const fileSizeRaw = Number(item.fileSize ?? item.file_size ?? item.size)
      return {
        id: idRaw || `${placeholder}-${filePath}-${idx}`,
        filePath,
        placeholder,
        strategy: strategyRaw || undefined,
        totalChars:
          Number.isFinite(totalCharsRaw) && totalCharsRaw > 0 ? Math.floor(totalCharsRaw) : undefined,
        totalLines:
          Number.isFinite(totalLinesRaw) && totalLinesRaw > 0 ? Math.floor(totalLinesRaw) : undefined,
        fileHash: /^[0-9a-f]{64}$/.test(fileHashRaw) ? fileHashRaw : undefined,
        fileSize: Number.isFinite(fileSizeRaw) && fileSizeRaw > 0 ? Math.floor(fileSizeRaw) : undefined,
      } as FileMentionFragment
    })
    .filter((item): item is FileMentionFragment => Boolean(item))
}

const extractTrailingFileMentionQuery = (value: string) => {
  const text = String(value || '')
  const match = /(?:^|\s)@([^\s@]*)$/.exec(text)
  if (!match) return null
  const full = match[0] || ''
  const atOffsetInFull = full.lastIndexOf('@')
  if (atOffsetInFull < 0) return null
  const atStart = match.index + atOffsetInFull
  const query = String(match[1] || '')
  return {
    atStart,
    end: text.length,
    query,
  }
}

const getFileExtension = (filePath?: string) => {
  const normalized = String(filePath || '').trim().toLowerCase()
  if (!normalized) return ''
  const index = normalized.lastIndexOf('.')
  if (index < 0 || index === normalized.length - 1) return ''
  return normalized.slice(index)
}

const buildMarkdownCompileResult = (
  filePath: string,
  content: string,
): DocStudioAPI.CompileResult => {
  const text = String(content || '')
  const errors: string[] = []
  const warnings: string[] = []

  const fencedCodeCount = (text.match(/^\s*```/gm) || []).length
  if (fencedCodeCount % 2 !== 0) {
    errors.push('检测到未闭合的 Markdown 代码块围栏（```）。')
  }

  const hasTitle = /^\s*#\s+\S+/m.test(text)
  if (!hasTitle && text.trim().length > 0) {
    warnings.push('文档缺少一级标题（# 标题），建议补充。')
  }

  const veryLongLines = text
    .split('\n')
    .map((line, idx) => ({ lineNo: idx + 1, len: line.length }))
    .filter((item) => item.len > 240)
    .slice(0, 5)
  if (veryLongLines.length > 0) {
    warnings.push(
      `存在较长行（>240 字符），示例行号：${veryLongLines.map((item) => item.lineNo).join(', ')}`,
    )
  }

  const success = errors.length === 0
  const warningText = warnings.length ? `\nWarnings:\n- ${warnings.join('\n- ')}` : ''
  const errorText = errors.length ? `\nErrors:\n- ${errors.join('\n- ')}` : ''
  const log = [
    `File: ${filePath}`,
    `Chars: ${text.length}`,
    `Lines: ${text.split('\n').length}`,
    `Fenced code blocks: ${Math.floor(fencedCodeCount / 2)}`,
    warningText,
    errorText,
  ]
    .filter(Boolean)
    .join('\n')

  return {
    success,
    summary: success ? `Markdown 检查通过：${filePath}` : `Markdown 检查失败：${filePath}`,
    data: {
      compiled: success,
      compile_format: 'markdown',
      target_path: filePath,
      preview_source: text,
      errors,
      warnings,
      logs: [
        {
          command: 'markdown_syntax_check',
          returncode: success ? 0 : 1,
          log,
        },
      ],
    },
    error: success ? undefined : errors[0] || 'Markdown 检查失败',
  }
}

type MentionTagClickTarget = {
  type: 'selection' | 'file'
  placeholder: string
  filePath?: string
  start?: number
  end?: number
}

const renderPromptWithMentionTags = (
  content: string,
  selectionFragments: SelectionFragment[],
  fileMentions: FileMentionFragment[],
  onMentionClick?: (target: MentionTagClickTarget) => void,
): React.ReactNode => {
  const text = String(content || '')
  if (!text) return text
  const hasPlaceholder = new RegExp(COMPOSER_PLACEHOLDER_REGEX.source).test(text)
  if (!hasPlaceholder) return text

  const regex = new RegExp(COMPOSER_PLACEHOLDER_REGEX.source, 'g')
  const selectionMap = new Map(selectionFragments.map((item) => [item.placeholder, item]))
  const fileMentionMap = new Map(fileMentions.map((item) => [item.placeholder, item]))
  const nodes: React.ReactNode[] = []
  let lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = regex.exec(text)) !== null) {
    const placeholder = match[0]
    const index = match.index
    if (index > lastIndex) {
      nodes.push(text.slice(lastIndex, index))
    }
    const isFileMention = placeholder.startsWith('@file')
    const mentionTarget: MentionTagClickTarget = isFileMention
      ? {
          type: 'file',
          placeholder,
          filePath: fileMentionMap.get(placeholder)?.filePath,
        }
      : {
          type: 'selection',
          placeholder,
          filePath: selectionMap.get(placeholder)?.filePath,
          start: selectionMap.get(placeholder)?.start,
          end: selectionMap.get(placeholder)?.end,
        }
    const clickable = Boolean(onMentionClick && mentionTarget.filePath)
    const titleParts = [placeholder]
    if (isFileMention) {
      const mention = fileMentionMap.get(placeholder)
      if (mention?.filePath) titleParts.push(mention.filePath)
      if (mention?.strategy) titleParts.push(mention.strategy)
      if (mention?.totalLines) titleParts.push(`${mention.totalLines} 行`)
    } else {
      const fragment = selectionMap.get(placeholder)
      if (fragment?.filePath) titleParts.push(fragment.filePath)
      if (fragment?.text) titleParts.push(`${fragment.text.length} 字符`)
    }
    nodes.push(
      <span
        key={`${placeholder}-${index}`}
        className={
          `${isFileMention
            ? 'doc-studio__chat-inline-selection doc-studio__chat-inline-selection--file'
            : 'doc-studio__chat-inline-selection'}${clickable ? ' doc-studio__chat-inline-selection--clickable' : ''}`
        }
        title={titleParts.join(' · ')}
        role={clickable ? 'button' : undefined}
        tabIndex={clickable ? 0 : undefined}
        onClick={
          clickable
            ? (event) => {
                event.preventDefault()
                event.stopPropagation()
                onMentionClick?.(mentionTarget)
              }
            : undefined
        }
        onKeyDown={
          clickable
            ? (event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  event.stopPropagation()
                  onMentionClick?.(mentionTarget)
                }
              }
            : undefined
        }
      >
        <FileTextOutlined />
        <span>{placeholder}</span>
      </span>,
    )
    lastIndex = index + placeholder.length
  }
  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex))
  }
  return nodes
}

type InteractionMode = 'ask' | 'agent'

type ChatImageAttachment = {
  id: string
  name: string
  mimeType: string
  size: number
  dataUrl: string
}

type ReEditDraft = {
  messageId: string
  msgIndex: number
  prompt: string
  runId: string
  beforeMessageId: string
  images: ChatImageAttachment[]
  selections: SelectionFragment[]
  fileMentions: FileMentionFragment[]
}

type PendingSendDraft = {
  userMessageId: string
  traceId: string
  prompt: string
  images: ChatImageAttachment[]
  selections: SelectionFragment[]
  fileMentions: FileMentionFragment[]
  committed: boolean
  commitReason?: 'delta' | 'tool'
}

type ContextMenuTargetType = 'workspace' | 'directory' | 'file'

type ContextMenuAction = {
  key: string
  label: string
  icon: React.ReactNode
  disabled?: boolean
  danger?: boolean
  separated?: boolean
  onClick: () => void
}

type CompileLogGroup = {
  command: string
  returncode: number
  log: string
  count: number
  firstIndex: number
}

type LiveTimelineLevel = 'info' | 'warning' | 'error'

type LiveTimelineEntry = {
  id: string
  sequence: number
  eventType: string
  text: string
  level: LiveTimelineLevel
  timestamp: number
}

const MAX_CHAT_IMAGE_COUNT = 4
const MAX_CHAT_IMAGE_FILE_SIZE = 6 * 1024 * 1024 // 6MB
const MAX_SELECTION_COUNT = 8
const SELECTION_PREVIEW_CHARS = 360
const MAX_FILE_MENTION_COUNT = 8
const MAX_FILE_MENTION_CANDIDATES = 8

const DASHSCOPE_TEXT_MODEL_OPTIONS = [
  { label: 'qwen-plus', value: 'qwen-plus' },
  { label: 'qwen3-max', value: 'qwen3-max' },
  { label: 'qwen-max', value: 'qwen-max' },
  { label: 'qwen-turbo', value: 'qwen-turbo' },
] as const

const DASHSCOPE_VISION_MODEL_OPTIONS = [
  { label: 'qwen-vl-max', value: 'qwen-vl-max' },
  { label: 'qwen-vl-plus', value: 'qwen-vl-plus' },
] as const

const DASHSCOPE_MODEL_OPTIONS = [
  ...DASHSCOPE_TEXT_MODEL_OPTIONS,
  ...DASHSCOPE_VISION_MODEL_OPTIONS,
] as const

const OPENAI_MODEL_OPTIONS = [
  { label: 'gpt-5.2', value: 'gpt-5.2' },
  { label: 'gpt-5', value: 'gpt-5' },
  { label: 'gpt-5-mini', value: 'gpt-5-mini' },
  { label: 'gpt-4.1', value: 'gpt-4.1' },
  { label: 'gpt-4o', value: 'gpt-4o' },
] as const

type LlmProviderValue = 'dashscope' | 'openai'
type LlmModelValue = string
type LlmModelOption = {
  label: string
  value: string
  provider: LlmProviderValue
  isVision: boolean
  available?: boolean
  status?: string
  reason?: string | null
  contextWindow?: number | null
}

const DEFAULT_DASHSCOPE_MODEL = 'qwen3-max'
const DEFAULT_DASHSCOPE_VISION_MODEL = 'qwen-vl-max'
const DEFAULT_OPENAI_MODEL = 'gpt-5.2'
const DEFAULT_OPENAI_VISION_MODEL = 'gpt-4o'
const DOC_STUDIO_LAST_USED_USER_KB_ID_STORAGE_KEY = 'doc_studio_last_user_kb_id'
const DASHSCOPE_VISION_MODEL_SET = new Set<string>(
  DASHSCOPE_VISION_MODEL_OPTIONS.map((item) => item.value),
)
const OPENAI_VISION_MODEL_SET = new Set<string>(['gpt-4o'])
const LLM_MODEL_OPTIONS: LlmModelOption[] = [
  ...DASHSCOPE_MODEL_OPTIONS.map((item) => ({
    label: `通义 · ${item.label}`,
    value: item.value,
    provider: 'dashscope' as const,
    isVision: DASHSCOPE_VISION_MODEL_SET.has(item.value),
  })),
  ...OPENAI_MODEL_OPTIONS.map((item) => ({
    label: `OpenAI · ${item.label}`,
    value: item.value,
    provider: 'openai' as const,
    isVision: OPENAI_VISION_MODEL_SET.has(item.value),
  })),
]
const LLM_MODEL_OPTION_MAP = new Map<string, LlmModelOption>(
  LLM_MODEL_OPTIONS.map((item) => [item.value, item]),
)
const buildLlmModelOptionsFromCatalog = (
  catalog: LlmModelCatalog | null,
): LlmModelOption[] => {
  const remote = (catalog?.models ?? [])
    .filter((item) => item.provider === 'dashscope' || item.provider === 'openai')
    .map((item) => ({
      label: item.label,
      value: item.model,
      provider: item.provider,
      isVision: Boolean(item.isVision),
      available: item.available,
      status: item.status,
      reason: item.reason,
      contextWindow: item.contextWindow,
    }))
  return remote.length ? remote : LLM_MODEL_OPTIONS
}
const MIN_LEFT_SIDER_WIDTH = 200
const MAX_LEFT_SIDER_WIDTH = 600
const MIN_RIGHT_SIDER_WIDTH = 260
const MAX_RIGHT_SIDER_WIDTH = 800
const MIN_CENTER_WIDTH = 420

const normalizeLlmProvider = (value: unknown): LlmProviderValue => {
  const normalized = String(value || '').trim().toLowerCase()
  return normalized === 'openai' ? 'openai' : 'dashscope'
}

const resolveProviderByModel = (value: unknown): LlmProviderValue => {
  if (typeof value === 'string') {
    return LLM_MODEL_OPTION_MAP.get(value)?.provider || 'dashscope'
  }
  return 'dashscope'
}

const defaultModelByProvider = (provider: LlmProviderValue): LlmModelValue =>
  provider === 'openai' ? DEFAULT_OPENAI_MODEL : DEFAULT_DASHSCOPE_MODEL

const defaultVisionModelByProvider = (provider: LlmProviderValue): LlmModelValue =>
  provider === 'openai' ? DEFAULT_OPENAI_VISION_MODEL : DEFAULT_DASHSCOPE_VISION_MODEL

const isVisionModel = (value: string) => Boolean(LLM_MODEL_OPTION_MAP.get(value)?.isVision)

const resolveModelLabel = (value: string) => LLM_MODEL_OPTION_MAP.get(value)?.label || value

const estimateLabelUnits = (text: string) =>
  Array.from(text).reduce((sum, ch) => sum + (/[\u4e00-\u9fff]/.test(ch) ? 1.85 : 1), 0)

const calcCompactSelectWidth = (label: string, minPx: number, maxPx: number) => {
  const width = Math.round(38 + estimateLabelUnits(label) * 8.6)
  return `${Math.max(minPx, Math.min(maxPx, width))}px`
}

const readLastUsedKnowledgeBaseId = () => {
  if (typeof window === 'undefined') return null
  const raw = localStorage.getItem(DOC_STUDIO_LAST_USED_USER_KB_ID_STORAGE_KEY)
  const parsed = Number(raw)
  if (!Number.isFinite(parsed) || parsed <= 0) return null
  return Math.floor(parsed)
}

const persistLastUsedKnowledgeBaseId = (kbId: number) => {
  if (typeof window === 'undefined') return
  const numeric = Number(kbId)
  if (!Number.isFinite(numeric) || numeric <= 0) return
  localStorage.setItem(
    DOC_STUDIO_LAST_USED_USER_KB_ID_STORAGE_KEY,
    String(Math.floor(numeric)),
  )
}

const resolvePreferredKnowledgeBaseId = (
  available: Array<{ id: number }>,
  candidates: Array<number | null | undefined> = [],
) => {
  if (!available.length) return null
  const preferred = [...candidates, readLastUsedKnowledgeBaseId()]
  for (const candidate of preferred) {
    const numeric = Number(candidate)
    if (!Number.isFinite(numeric) || numeric <= 0) continue
    const matched = available.find((item) => item.id === numeric)
    if (matched) return matched.id
  }
  return available[0].id
}

const normalizeCount = (value: unknown) => {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return 0
  return Math.max(0, Math.round(numeric))
}

const normalizeWorkspacePath = (value: string) =>
  value.replace(/\\/g, '/').replace(/^\/+|\/+$/g, '')

const splitWorkspacePath = (path: string) => {
  const normalized = normalizeWorkspacePath(path)
  const segments = normalized.split('/').filter(Boolean)
  const name = segments[segments.length - 1] || ''
  const parentPath = segments.slice(0, -1).join('/')
  return { normalized, name, parentPath }
}

const remapPathWithPrefix = (
  inputPath: string,
  sourcePath: string,
  targetPath: string,
  sourceType: 'file' | 'directory',
) => {
  if (!inputPath) return inputPath
  if (sourceType === 'file') {
    return inputPath === sourcePath ? targetPath : inputPath
  }
  if (inputPath === sourcePath) return targetPath
  const sourcePrefix = `${sourcePath}/`
  if (inputPath.startsWith(sourcePrefix)) {
    return `${targetPath}/${inputPath.slice(sourcePrefix.length)}`
  }
  return inputPath
}

type ReadonlyFileNode = Readonly<
  Omit<DocStudioAPI.FileNode, 'children'>
> & {
  readonly children?: ReadonlyArray<ReadonlyFileNode>
}

const cloneFileNodes = (
  nodes: ReadonlyArray<ReadonlyFileNode>,
): DocStudioAPI.FileNode[] =>
  nodes.map((node) => ({
    ...node,
    children: node.children ? cloneFileNodes(node.children) : undefined,
  }))

const buildTreeData = (
  nodes: ReadonlyArray<ReadonlyFileNode>,
  renderTitle: (node: ReadonlyFileNode) => React.ReactNode,
): DataNode[] =>
  nodes.map((node) => ({
    key: node.path,
    title: renderTitle(node),
    isLeaf: node.type === 'file',
    children: node.children ? buildTreeData(node.children, renderTitle) : undefined,
  }))

const LatexEditorPage = () => {
  const params = useParams<{ workspaceId?: string }>()
  const navigate = useNavigate()
  const snap = useSnapshot(docStudioState)
  const [prompt, setPrompt] = useState('')
  const [chatLoading, setChatLoading] = useState(false)
  const [reEditDraft, setReEditDraft] = useState<ReEditDraft | null>(null)
  const [reEditSubmitting, setReEditSubmitting] = useState(false)
  const asyncMode = true
  const [selections, setSelections] = useState<SelectionFragment[]>([])
  const [fileMentions, setFileMentions] = useState<FileMentionFragment[]>([])
  const [fileMentionQuery, setFileMentionQuery] = useState('')
  const [fileMentionRange, setFileMentionRange] = useState<{ start: number; end: number } | null>(null)
  const [fileMentionActiveIndex, setFileMentionActiveIndex] = useState(0)
  const [workspaceModalOpen, setWorkspaceModalOpen] = useState(false)
  const [newWorkspaceName, setNewWorkspaceName] = useState('')
  // ScriptLens 不区分 latex / markdown，但 setter 还在 onCancel/重置流程里用到，
  // 留个 _ 前缀的占位避免 TS noUnusedLocals 报错。
  const [, setNewWorkspaceType] = useState<'latex' | 'markdown'>('latex')
  const [workspaceSubmitting, setWorkspaceSubmitting] = useState(false)
  // ScriptLens 上传单文件即创建剧本工作区
  const [newWorkspaceFile, setNewWorkspaceFile] = useState<File | null>(null)
  // ScriptLens M3：场景改写 Modal（PRD §7 P1，POST /rewrite + AgentDiffReview in-place）
  const [rewriteModalOpen, setRewriteModalOpen] = useState(false)
  const [rewriteDimension, setRewriteDimension] = useState<
    'opening_hook' | 'reward_density' | 'motivation' | 'pacing' | 'risk'
  >('motivation')
  const [rewriteIssue, setRewriteIssue] = useState('')
  const [rewriteSubmitting, setRewriteSubmitting] = useState(false)
  const [fileModalOpen, setFileModalOpen] = useState(false)
  const [fileModalType, setFileModalType] = useState<'file' | 'directory'>('file')
  const [fileModalPath, setFileModalPath] = useState('')
  const [fileModalContent, setFileModalContent] = useState('')
  const [fileSubmitting, setFileSubmitting] = useState(false)
  const [renameModalOpen, setRenameModalOpen] = useState(false)
  const [renameSubmitting, setRenameSubmitting] = useState(false)
  const [renameSourcePath, setRenameSourcePath] = useState('')
  const [renameSourceType, setRenameSourceType] = useState<'file' | 'directory'>('file')
  const [renameNameInput, setRenameNameInput] = useState('')
  const [uploading, setUploading] = useState(false)
  const [chatImageAttachments, setChatImageAttachments] = useState<ChatImageAttachment[]>([])
  const [chatImageProcessing, setChatImageProcessing] = useState(false)
  const [historyDropdownOpen, setHistoryDropdownOpen] = useState(false)
  const [historySearchKeyword, setHistorySearchKeyword] = useState('')
  const [, setSessionTitleVersion] = useState(0)
  const [rightTab, setRightTab] = useState<'chat' | 'history' | 'compile'>(() => {
    if (typeof window === 'undefined') return 'chat'
    const saved = localStorage.getItem('doc_studio_right_tab')
    return (saved === 'chat' || saved === 'history' || saved === 'compile') ? saved : 'chat'
  })
  const [rightPanelClosed, setRightPanelClosed] = useState(() => {
    if (typeof window === 'undefined') return false
    return localStorage.getItem('doc_studio_right_panel_closed') === 'true'
  })
  const [leftPanelClosed, setLeftPanelClosed] = useState(() => {
    if (typeof window === 'undefined') return false
    return localStorage.getItem('doc_studio_left_panel_closed') === 'true'
  })
  const [llmModel, setLlmModel] = useState<LlmModelValue>(DEFAULT_OPENAI_MODEL)
  const [llmModelCatalog, setLlmModelCatalog] = useState<LlmModelCatalog | null>(null)
  const [llmModelCatalogLoading, setLlmModelCatalogLoading] = useState(false)
  const llmModelOptions = useMemo(
    () => buildLlmModelOptionsFromCatalog(llmModelCatalog),
    [llmModelCatalog],
  )
  const llmModelOptionMap = useMemo(
    () => new Map<string, LlmModelOption>(llmModelOptions.map((item) => [item.value, item])),
    [llmModelOptions],
  )
  const llmModelSet = useMemo(
    () => new Set<string>(llmModelOptions.map((item) => item.value)),
    [llmModelOptions],
  )
  const defaultRuntimeModelByProvider = useCallback(
    (provider: LlmProviderValue): LlmModelValue => {
      const catalogDefault = llmModelCatalog?.defaultModel
      if (catalogDefault && llmModelSet.has(catalogDefault)) return catalogDefault
      const matched = llmModelOptions.find(
        (item) => item.provider === provider && !item.isVision && item.available !== false,
      )
      return matched?.value || defaultModelByProvider(provider)
    },
    [llmModelCatalog?.defaultModel, llmModelOptions, llmModelSet],
  )
  const defaultRuntimeVisionModelByProvider = useCallback(
    (provider: LlmProviderValue): LlmModelValue => {
      const catalogDefault = llmModelCatalog?.defaultVisionModel
      if (catalogDefault && llmModelSet.has(catalogDefault)) return catalogDefault
      const matched = llmModelOptions.find(
        (item) => item.provider === provider && item.isVision && item.available !== false,
      )
      return matched?.value || defaultVisionModelByProvider(provider)
    },
    [llmModelCatalog?.defaultVisionModel, llmModelOptions, llmModelSet],
  )
  const resolveRuntimeProviderByModel = useCallback(
    (value: unknown): LlmProviderValue => {
      if (typeof value === 'string') {
        return llmModelOptionMap.get(value)?.provider || resolveProviderByModel(value)
      }
      return normalizeLlmProvider(llmModelCatalog?.preferredProvider)
    },
    [llmModelCatalog?.preferredProvider, llmModelOptionMap],
  )
  const normalizeRuntimeLlmModel = useCallback(
    (value: unknown, providerHint?: unknown): LlmModelValue => {
      if (typeof value === 'string' && llmModelSet.has(value)) {
        const option = llmModelOptionMap.get(value)
        if (option?.available !== false) return value
      }
      const fallbackProvider =
        providerHint === undefined
          ? normalizeLlmProvider(llmModelCatalog?.preferredProvider)
          : normalizeLlmProvider(providerHint)
      return defaultRuntimeModelByProvider(fallbackProvider)
    },
    [
      defaultRuntimeModelByProvider,
      llmModelCatalog?.preferredProvider,
      llmModelOptionMap,
      llmModelSet,
    ],
  )
  const isRuntimeVisionModel = useCallback(
    (value: string) =>
      Boolean(llmModelOptionMap.get(value)?.isVision ?? isVisionModel(value)),
    [llmModelOptionMap],
  )
  const resolveRuntimeModelLabel = useCallback(
    (value: string) => llmModelOptionMap.get(value)?.label || resolveModelLabel(value),
    [llmModelOptionMap],
  )
  const [interactionMode, setInteractionMode] = useState<InteractionMode>('agent')
  const [ragEnabled, setRagEnabled] = useState<boolean>(() => {
    if (typeof window === 'undefined') return true
    return localStorage.getItem('doc_studio_rag_enabled') !== 'false'
  })
  const [llmOptionsReady, setLlmOptionsReady] = useState(false)
  const [debugModalOpen, setDebugModalOpen] = useState(false)
  const [debugData, setDebugData] = useState<{
    items: { message_id: string; content_length: number; newline_count: number; double_newline_count: number; triple_plus_newline_count: number; raw_repr_sample: string; raw_with_markers: string }[]
    error?: string
  } | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const chatImageInputRef = useRef<HTMLInputElement | null>(null)
  const editorRef = useRef<any>(null)
  // ScriptLens 适配后是 ScriptLensAgentStream（EventSource 接口子集），
  // 但 UI 全程只用 addEventListener / close，结构兼容 EventSource。
  // 这里 listener 收窄为函数签名（不支持 EventListenerObject），跟 sseClient 对齐。
  const asyncStreamRef = useRef<{
    addEventListener: (type: string, listener: (event: MessageEvent) => void) => void
    removeEventListener: (type: string, listener: (event: MessageEvent) => void) => void
    close: () => void
    readyState?: number
  } | null>(null)
  const chatMessagesEndRef = useRef<HTMLDivElement | null>(null)
  const chatMessagesContainerRef = useRef<HTMLDivElement | null>(null)
  const chatInputContainerRef = useRef<HTMLDivElement | null>(null)
  const reEditContainerRef = useRef<HTMLDivElement | null>(null)
  const sessionTitlesRef = useRef<Record<string, string>>({})
  const autoTitledSessionRef = useRef<Record<string, true>>({})
  const [chatToolbarCompact, setChatToolbarCompact] = useState(false)
  const lastAutoScrollMessageIdRef = useRef<string | null>(null)
  const promptInputRef = useRef<TextAreaRef | null>(null)
  const promptWrapperRef = useRef<HTMLDivElement | null>(null)
  const llmOptionsAppliedRef = useRef<string>('')
  const saveInFlightRef = useRef(false)
  
  // Diff 审阅相关状态
  const [agentDiffReviewOpen, setAgentDiffReviewOpen] = useState(false)
  const [diffModalOpen, setDiffModalOpen] = useState(false)
  const [allFileDiffs, setAllFileDiffs] = useState<DocStudioAPI.FileDiff[]>([])
  const [currentDiffIndex, setCurrentDiffIndex] = useState(0)
  const [lastOperationId, setLastOperationId] = useState<string | null>(null)
  const [diffOperationId, setDiffOperationId] = useState<string | null>(null)
  const [diffModalContext, setDiffModalContext] = useState<'agent' | 'timeline'>('agent')
  const [undoingLastApply, setUndoingLastApply] = useState(false)
  const [diffReverting, setDiffReverting] = useState(false)
  const [operationHistoryLoading, setOperationHistoryLoading] = useState(false)
  const [operationHistory, setOperationHistory] = useState<DocStudioAPI.OperationSummary[]>([])
  const [revertingOperationId, setRevertingOperationId] = useState<string | null>(null)
  const [systemStatsLoading, setSystemStatsLoading] = useState(false)
  const [llmHealth, setLlmHealth] = useState<DocStudioAPI.LlmHealthSummary | null>(null)
  const [metricsSummary, setMetricsSummary] = useState<DocStudioAPI.MetricsSummary | null>(null)
  const [systemStatusOpen, setSystemStatusOpen] = useState(false)
  const [resolvedOriginal, setResolvedOriginal] = useState('')
  const [resolvedModified, setResolvedModified] = useState('')
  const [lineChanges, setLineChanges] = useState<any[]>([])
  const [currentHunkIndex, setCurrentHunkIndex] = useState(0)
  const [liveAgentStatus, setLiveAgentStatus] = useState('')
  const [liveAgentTimeline, setLiveAgentTimeline] = useState<LiveTimelineEntry[]>([])
  const [liveAgentPreviewText, setLiveAgentPreviewText] = useState('')
  const [liveDeltaCharCount, setLiveDeltaCharCount] = useState(0)
  const [liveAgentElapsedSec, setLiveAgentElapsedSec] = useState(0)
  const diffEditorRef = useRef<any>(null)
  const diffHunkDecorationsRef = useRef<string[]>([])
  const agentDiffReviewRef = useRef<AgentDiffReviewRef | null>(null)
  const diffEditorListenerRef = useRef<Array<{ dispose: () => void }>>([])
  const asyncRunResolvedRef = useRef(false)
  const liveDeltaStartedRef = useRef(false)
  const activeRunIdRef = useRef<string | null>(null)
  const pendingSendRef = useRef<PendingSendDraft | null>(null)
  const stopRequestedRef = useRef(false)
  const skipNextComposerClearRef = useRef(false)
  const seenLiveEventIdsRef = useRef<Set<string>>(new Set())
  const handledInteractionIdsRef = useRef<Set<string>>(new Set())
  const lastLiveEventSequenceRef = useRef(-1)
  const liveOutputRef = useRef<HTMLDivElement | null>(null)
  const livePreviewLines = useMemo(
    () => buildLivePreviewLines(liveAgentPreviewText, 4, 64),
    [liveAgentPreviewText],
  )
  
  // 侧边栏宽度持久化到 localStorage
  const [leftSiderWidth, setLeftSiderWidth] = useState(() => {
    const saved = localStorage.getItem('latex_editor_left_sider_width')
    return saved ? parseInt(saved, 10) : 260
  })
  const [rightSiderWidth, setRightSiderWidth] = useState(() => {
    const saved = localStorage.getItem('latex_editor_right_sider_width')
    return saved ? parseInt(saved, 10) : 360
  })
  
  const [isDraggingLeft, setIsDraggingLeft] = useState(false)
  const [isDraggingRight, setIsDraggingRight] = useState(false)
  
  const preferredKbFromUrl = useMemo(() => {
    if (typeof window === 'undefined') return null
    const raw = new URLSearchParams(window.location.search).get('kb_id')
    if (!raw) return null
    const parsed = Number(raw)
    return Number.isFinite(parsed) ? parsed : null
  }, [])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const raw = localStorage.getItem('doc_studio_llm_options')
    if (!raw) return
    try {
      const parsed = JSON.parse(raw)
      setLlmModel(normalizeRuntimeLlmModel(parsed?.llm_model, parsed?.llm_provider))
      const parsedMode = parsed?.interaction_mode
      if (parsedMode === 'ask' || parsedMode === 'agent') {
        setInteractionMode(parsedMode)
      }
    } catch (error) {
      console.warn('Failed to load Doc Studio LLM options', error)
    }
  }, [normalizeRuntimeLlmModel])

  useEffect(() => {
    let cancelled = false
    setLlmModelCatalogLoading(true)
    fetchLlmModels({ loading: false, errorToast: false })
      .then(({ data }) => {
        if (cancelled) return
        const nextCatalog = data ?? null
        setLlmModelCatalog(nextCatalog)
        const nextOptions = buildLlmModelOptionsFromCatalog(nextCatalog)
        const nextSet = new Set(nextOptions.map((item) => item.value))
        const nextMap = new Map(nextOptions.map((item) => [item.value, item]))
        const fallback =
          nextCatalog?.defaultModel ||
          nextOptions.find((item) => item.available !== false && !item.isVision)?.value ||
          DEFAULT_OPENAI_MODEL
        setLlmModel((current) => {
          const option = nextMap.get(current)
          if (option && option.available !== false) return current
          if (nextSet.has(fallback)) return fallback
          return normalizeRuntimeLlmModel(fallback, nextCatalog?.preferredProvider)
        })
      })
      .catch((error) => {
        console.warn('Failed to load LLM model catalog', error)
      })
      .finally(() => {
        if (!cancelled) setLlmModelCatalogLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const provider = resolveRuntimeProviderByModel(llmModel)
    const payload = {
      llm_provider: provider,
      llm_model: llmModel,
      interaction_mode: interactionMode,
    }
    localStorage.setItem('doc_studio_llm_options', JSON.stringify(payload))
  }, [interactionMode, llmModel, resolveRuntimeProviderByModel])

  const llmOptions = useMemo(() => {
    const provider = resolveRuntimeProviderByModel(llmModel)
    return {
      llm_provider: provider,
      llm_model: llmModel,
      interaction_mode: interactionMode,
    }
  }, [interactionMode, llmModel, resolveRuntimeProviderByModel])

  const llmOptionsConfig = useMemo(() => {
    const provider = resolveRuntimeProviderByModel(llmModel)
    return {
      llm_provider: provider,
      llm_model: llmModel,
      interaction_mode: interactionMode,
    }
  }, [interactionMode, llmModel, resolveRuntimeProviderByModel])

  const applyLlmOptionsFromConfig = useCallback(
    (config?: Record<string, any>) => {
      if (!config) return
      const raw =
        config.llm_options && typeof config.llm_options === 'object' ? config.llm_options : config
      const hasAny = raw.llm_provider || raw.llm_model || raw.interaction_mode
      if (!hasAny) return

      const nextProvider = normalizeLlmProvider(raw.llm_provider)
      const nextModel = normalizeRuntimeLlmModel(raw.llm_model ?? llmModel, nextProvider)
      const nextInteractionMode: InteractionMode =
        raw.interaction_mode === 'ask' ? 'ask' : 'agent'

      const normalized = {
        llm_provider: resolveRuntimeProviderByModel(nextModel),
        llm_model: nextModel,
        interaction_mode: nextInteractionMode,
      }
      llmOptionsAppliedRef.current = JSON.stringify(normalized)
      setLlmModel(nextModel)
      setInteractionMode(nextInteractionMode)
    },
    [llmModel, normalizeRuntimeLlmModel, resolveRuntimeProviderByModel],
  )

  useEffect(() => {
    setLlmOptionsReady(false)
    llmOptionsAppliedRef.current = ''
  }, [snap.workspaceId])

  useEffect(() => {
    setOperationHistory([])
  }, [snap.workspaceId])

  useEffect(() => {
    if (typeof window === 'undefined') return
    localStorage.setItem('doc_studio_rag_enabled', ragEnabled ? 'true' : 'false')
  }, [ragEnabled])

  useEffect(() => {
    if (!snap.workspaceId || !llmOptionsReady) return
    const serialized = JSON.stringify(llmOptionsConfig)
    if (serialized === llmOptionsAppliedRef.current) return
    llmOptionsAppliedRef.current = serialized
    updateWorkspace({
      workspaceId: snap.workspaceId,
      config: { llm_options: llmOptionsConfig },
    }, {
      // 模型/模式切换属于轻量偏好同步，不应触发全局 loading 闪烁
      loading: false,
      errorToast: false,
    }).catch((error) => {
      console.warn('Failed to persist LLM options', error)
    })
  }, [llmOptionsConfig, llmOptionsReady, snap.workspaceId])
  const preferredFileFromUrl = useMemo(() => {
    if (typeof window === 'undefined') return ''
    const raw = new URLSearchParams(window.location.search).get('file')
    return raw ? raw.trim() : ''
  }, [])
  const autoCompileFromUrl = useMemo(() => {
    if (typeof window === 'undefined') return false
    const raw = new URLSearchParams(window.location.search).get('auto_compile')
    if (!raw) return false
    const normalized = raw.trim().toLowerCase()
    return normalized === '1' || normalized === 'true' || normalized === 'yes'
  }, [])
  const autoCompileHandledRef = useRef(false)
  const [knowledgeBases, setKnowledgeBases] = useState<DocStudioAPI.KnowledgeBaseSummary[]>([])
  const [knowledgeLoading, setKnowledgeLoading] = useState(false)
  // ??????????????????
  const [selectedKnowledgeBaseId, setSelectedKnowledgeBaseId] = useState<number | null>(null)
  const selectedKnowledgeBaseIdRef = useRef<number | null>(null)
  const [feedbackSubmitting, setFeedbackSubmitting] = useState<Record<string, boolean>>({})
  
  // ???????
  const [contextMenuVisible, setContextMenuVisible] = useState(false)
  const [contextMenuPosition, setContextMenuPosition] = useState({ x: 0, y: 0 })
  const [contextMenuPath, setContextMenuPath] = useState<string>('')
  const [contextMenuType, setContextMenuType] = useState<ContextMenuTargetType>('file')
  
  // Tree ??????????????
  const [expandedKeys, setExpandedKeys] = useState<React.Key[]>([])
  const [treeFocusPath, setTreeFocusPath] = useState('')
  const [hoveredTreePath, setHoveredTreePath] = useState('')

  // Notebook 复用 Doc Studio 的工作区存储，但在 Doc Studio 视图里必须当作不可见资源，
  // 仅可通过 /doc-studio/notebook 显式访问，不出现在 Doc Studio 工作区下拉里。
  const docStudioWorkspaces = useMemo(
    () => snap.workspaces.filter((item) => item.workspaceId !== NOTEBOOK_WORKSPACE_ID),
    [snap.workspaces],
  )
  const workspaceOptions = useMemo(
    () =>
      docStudioWorkspaces.map((item) => ({
        label: item.name,
        value: item.workspaceId,
      })),
    [docStudioWorkspaces],
  )
  const activeWorkspaceName = useMemo(() => {
    const activeWorkspace = snap.workspaces.find((item) => item.workspaceId === snap.workspaceId)
    const workspaceName = activeWorkspace?.name?.trim()
    if (workspaceName) return workspaceName
    if (snap.workspaceId?.trim()) return snap.workspaceId.trim()
    return 'workspace'
  }, [snap.workspaceId, snap.workspaces])
  const explorerTitle = useMemo(() => activeWorkspaceName.toLocaleUpperCase(), [activeWorkspaceName])

  const isLatexWorkspace = useMemo(() => {
    const config = snap.workspaceConfig || {}
    const workspaceType = config.workspace_type || config.workspaceType
    const primaryFormat = config.primary_format || config.primaryFormat
    const mainFile = config.main_file || config.mainFile
    if (workspaceType === 'latex' || primaryFormat === 'latex') return true
    if (typeof mainFile === 'string' && mainFile.toLowerCase().endsWith('.tex')) return true
    if (snap.activeFilePath?.toLowerCase().endsWith('.tex')) return true
    return false
  }, [snap.activeFilePath, snap.workspaceConfig])

  const activeFileExtension = useMemo(
    () => getFileExtension(snap.activeFilePath),
    [snap.activeFilePath],
  )
  const isMarkdownActiveFile = activeFileExtension === '.md' || activeFileExtension === '.markdown'
  const isPlaintextActiveFile = activeFileExtension === '.txt'
  const supportsCompilePanel = isLatexWorkspace || isMarkdownActiveFile
  const compileActionTitle = useMemo(() => {
    if (isPlaintextActiveFile) return 'TXT 文件无需编译'
    if (isMarkdownActiveFile) return '编译 Markdown'
    return '编译'
  }, [isMarkdownActiveFile, isPlaintextActiveFile])

  const isNotebookWorkspace = useMemo(() => {
    const workspaceType = String(
      snap.workspaceConfig?.workspace_type || snap.workspaceConfig?.workspaceType || '',
    )
      .trim()
      .toLowerCase()
    return snap.workspaceId === NOTEBOOK_WORKSPACE_ID || workspaceType === 'notebook'
  }, [snap.workspaceConfig, snap.workspaceId])

  const notebookLockedPaths = useMemo(() => {
    if (!isNotebookWorkspace) return [] as string[]

    const configLocked =
      snap.workspaceConfig?.notebook_locked_paths || snap.workspaceConfig?.notebookLockedPaths
    const configAutoDir = String(
      snap.workspaceConfig?.notebook_auto_dir || snap.workspaceConfig?.notebookAutoDir || '',
    )
      .trim()
      .toLowerCase()

    const candidates = [
      ...(Array.isArray(configLocked) ? configLocked : []),
      ...NOTEBOOK_LOCKED_PATHS,
      configAutoDir,
    ]
    const deduped: string[] = []
    candidates.forEach((item) => {
      const normalized = normalizeWorkspacePath(String(item || '').toLowerCase())
      if (normalized && !deduped.includes(normalized)) {
        deduped.push(normalized)
      }
    })
    return deduped
  }, [isNotebookWorkspace, snap.workspaceConfig])

  const isNotebookSystemPath = useCallback(
    (rawPath: string, options?: { protectParents?: boolean }) => {
      if (!isNotebookWorkspace) return false
      const normalizedPath = normalizeWorkspacePath(String(rawPath || '').toLowerCase())
      if (!normalizedPath) return false
      return notebookLockedPaths.some((lockedPath) => {
        if (
          normalizedPath === lockedPath ||
          normalizedPath.startsWith(`${lockedPath}/`)
        ) {
          return true
        }
        if (options?.protectParents) {
          return lockedPath.startsWith(`${normalizedPath}/`)
        }
        return false
      })
    },
    [isNotebookWorkspace, notebookLockedPaths],
  )

  const clearAutoCompileFlagFromUrl = useCallback(() => {
    if (typeof window === 'undefined') return
    const url = new URL(window.location.href)
    if (!url.searchParams.has('auto_compile')) return
    url.searchParams.delete('auto_compile')
    const nextSearch = url.searchParams.toString()
    const nextUrl = `${url.pathname}${nextSearch ? `?${nextSearch}` : ''}${url.hash}`
    window.history.replaceState({}, '', nextUrl)
  }, [])

  const expandedWorkspaceInitRef = useRef('')

  // 仅在工作区首次加载文件树时默认展开目录；后续刷新保留用户的展开/收起状态。
  useEffect(() => {
    const collectDirectoryKeys = (nodes: ReadonlyArray<ReadonlyFileNode>): string[] => {
      const keys: string[] = []
      for (const node of nodes) {
        if (node.type === 'directory') {
          keys.push(node.path)
          if (node.children?.length) {
            keys.push(...collectDirectoryKeys(node.children))
          }
        }
      }
      return keys
    }

    const workspaceKey = String(snap.workspaceId || '')
    const allDirKeys = collectDirectoryKeys((snap.fileTree || []) as ReadonlyArray<ReadonlyFileNode>)

    if (!allDirKeys.length) {
      setExpandedKeys([])
      if (!workspaceKey) {
        expandedWorkspaceInitRef.current = ''
      }
      return
    }

    const isFirstTreeLoadForWorkspace = expandedWorkspaceInitRef.current !== workspaceKey
    setExpandedKeys((prev) => {
      if (isFirstTreeLoadForWorkspace) {
        return allDirKeys
      }
      const keySet = new Set(allDirKeys)
      return prev.filter((key) => keySet.has(String(key)))
    })

    if (isFirstTreeLoadForWorkspace) {
      expandedWorkspaceInitRef.current = workspaceKey
    }
  }, [snap.fileTree, snap.workspaceId])
  const knowledgeBaseOptions = useMemo(
    () =>
      // ??????????????????
      knowledgeBases
        .filter((item) => !item.is_ephemeral)
        .map((item) => ({
          label: item.name,
          value: item.id,
        })),
    [knowledgeBases],
  )
  const selectedKnowledgeBase = useMemo(
    () => knowledgeBases.find((item) => item.id === selectedKnowledgeBaseId) || null,
    [knowledgeBases, selectedKnowledgeBaseId],
  )
  const modeSelectWidth = useMemo(
    () => calcCompactSelectWidth(interactionMode === 'ask' ? 'Ask' : 'Agent', 58, 106),
    [interactionMode],
  )
  const modelSelectWidth = useMemo(() => {
    const label = resolveRuntimeModelLabel(llmModel)
    return calcCompactSelectWidth(label, 108, 220)
  }, [llmModel, resolveRuntimeModelLabel])
  const ragSelectWidth = useMemo(() => {
    const label = selectedKnowledgeBase?.name || '知识库'
    return calcCompactSelectWidth(label, 66, 150)
  }, [selectedKnowledgeBase?.name])
  const headerTabItems = useMemo(
    () =>
      snap.openedFiles.map((path) => ({
        key: path,
        label: (
          <span className="doc-studio__header-tab-label" title={path}>
            {path.split('/').pop()}
          </span>
        ),
      })),
    [snap.openedFiles],
  )
  const compileLogGroups = useMemo<CompileLogGroup[]>(() => {
    const logs = snap.compileResult?.data?.logs
    if (!Array.isArray(logs) || !logs.length) return []
    const grouped: Array<CompileLogGroup & { signature: string }> = []
    logs.forEach((item, index) => {
      const command = typeof item?.command === 'string' ? item.command : 'unknown'
      const returncode = Number.isFinite(Number(item?.returncode)) ? Number(item.returncode) : -1
      const rawLog = typeof item?.log === 'string' ? item.log : ''
      const highlightedLines = rawLog
        .split('\n')
        .map((line: string) => line.trim())
        .filter(
          (line: string) =>
            !!line &&
            (line.startsWith('!')
              || line.includes('Error')
              || line.includes('Warning')
              || line.includes('Missing character')),
        )
      const normalizedBody = highlightedLines.length > 0
        ? highlightedLines.join('\n')
        : rawLog.trim()
      const signature = `${command}::${returncode}::${normalizedBody}`
      const previous = grouped[grouped.length - 1]
      if (previous && previous.signature === signature) {
        previous.count += 1
        return
      }
      grouped.push({
        command,
        returncode,
        log: rawLog,
        count: 1,
        firstIndex: index,
        signature,
      })
    })
    return grouped.map(({ signature: _signature, ...rest }) => rest)
  }, [snap.compileResult?.data?.logs])

  const compileFormat = useMemo<'latex' | 'markdown' | 'unknown'>(() => {
    const explicit = String(snap.compileResult?.data?.compile_format || '').toLowerCase()
    if (explicit === 'latex') return 'latex'
    if (explicit === 'markdown') return 'markdown'
    if (snap.compileResult?.data?.pdf_path) return 'latex'
    if (isMarkdownActiveFile) return 'markdown'
    if (isLatexWorkspace) return 'latex'
    return 'unknown'
  }, [
    isLatexWorkspace,
    isMarkdownActiveFile,
    snap.compileResult?.data?.compile_format,
    snap.compileResult?.data?.pdf_path,
  ])

  const markdownCompilePreviewContent = useMemo(() => {
    if (compileFormat !== 'markdown') return ''
    const fromResult = snap.compileResult?.data?.preview_source
    if (typeof fromResult === 'string' && fromResult.length > 0) return fromResult
    const targetPath = String(snap.compileResult?.data?.target_path || '').trim()
    if (targetPath && snap.files[targetPath]) {
      return String(snap.files[targetPath]?.content || '')
    }
    const activePath = String(snap.activeFilePath || '').trim()
    if (isMarkdownActiveFile && activePath) {
      return String(snap.files[activePath]?.content || '')
    }
    return ''
  }, [
    compileFormat,
    isMarkdownActiveFile,
    snap.activeFilePath,
    snap.compileResult?.data?.preview_source,
    snap.compileResult?.data?.target_path,
    snap.files,
  ])

  const workspaceFilePaths = useMemo(
    () => collectAllFilePaths((snap.fileTree || []) as DocStudioAPI.FileNode[]),
    [snap.fileTree],
  )
  const fileMentionCandidates = useMemo(() => {
    if (!fileMentionRange) return []
    const keyword = fileMentionQuery.trim().toLowerCase()
    const scored = workspaceFilePaths
      .map((path) => {
        const normalizedPath = path.toLowerCase()
        const basename = path.split('/').pop()?.toLowerCase() || normalizedPath
        let score = 3
        if (!keyword) {
          score = 0
        } else if (basename.startsWith(keyword)) {
          score = 0
        } else if (basename.includes(keyword)) {
          score = 1
        } else if (normalizedPath.includes(keyword)) {
          score = 2
        }
        return { path, score }
      })
      .filter((item) => item.score < 3)
      .sort((a, b) => {
        if (a.score !== b.score) return a.score - b.score
        return a.path.localeCompare(b.path)
      })
      .slice(0, MAX_FILE_MENTION_CANDIDATES)
      .map((item) => item.path)
    return scored
  }, [fileMentionQuery, fileMentionRange, workspaceFilePaths])
  const fileMentionDropdownOpen = Boolean(fileMentionRange && fileMentionCandidates.length > 0)

  const insertPlaceholderAtCursor = useCallback((placeholder: string) => {
    // ????????????????????????????
    setPrompt((prev) => {
      if (!prev || prev.trim() === '') {
        return placeholder
      }
      // ??????????????????
      if (prev.endsWith(' ') || prev.endsWith('\n')) {
        return `${prev}${placeholder}`
      }
      return `${prev} ${placeholder}`
    })
  }, [])

  const addSelectionSnippet = useCallback(() => {
    // ??????????????????????
    const editor = editorRef.current
    if (!editor) {
      message.warning('编辑器尚未就绪')
      return
    }
    
    const selectionRanges = editor.getSelections() || []
    const targetRange = selectionRanges.find((range: any) => !range.isEmpty())
    
    if (!targetRange) {
      message.warning('请先在编辑器中选择文本')
      return
    }
    
    const model = editor.getModel()
    if (!model) {
      message.warning('编辑器模型未就绪')
      return
    }
    
    const rawText = model.getValueInRange(targetRange).trim()
    if (!rawText) {
      message.warning('选区为空')
      return
    }
    const text = buildSelectionPreview(rawText)
    
    // ???????????
    setSelections((prev) => {
      if (prev.length >= MAX_SELECTION_COUNT) {
        message.warning(`最多可引用 ${MAX_SELECTION_COUNT} 段选区`)
        return prev
      }
      const start = model.getOffsetAt(targetRange.getStartPosition())
      const end = model.getOffsetAt(targetRange.getEndPosition())
      const startPosition = targetRange.getStartPosition()
      const endPosition = targetRange.getEndPosition()
      const existed = prev.find(
        (item) =>
          item.filePath === snap.activeFilePath &&
          item.start === start &&
          item.end === end,
      )
      if (existed) {
        insertPlaceholderAtCursor(existed.placeholder)
        requestAnimationFrame(() => {
          promptInputDivRef.current?.focus()
        })
        return prev
      }
      const placeholder = `@selection${prev.length + 1}`
      const snippet: SelectionFragment = {
        id: generateId(),
        start,
        end,
        text,
        filePath: snap.activeFilePath,
        placeholder,
        startLine: startPosition.lineNumber,
        endLine: endPosition.lineNumber,
        startColumn: startPosition.column,
        endColumn: endPosition.column,
        totalChars: rawText.length,
        isRangeReference: true,
      }
      insertPlaceholderAtCursor(placeholder)
      
      // ??????????????????DOM ??????
      requestAnimationFrame(() => {
        promptInputDivRef.current?.focus()
      })
      
      return [...prev, snippet]
    })
  }, [insertPlaceholderAtCursor, snap.activeFilePath])

  const removeSelectionSnippet = useCallback(
    (placeholder: string) => {
      if (!selections.length) return
      const filtered = selections.filter((item) => item.placeholder !== placeholder)
      if (filtered.length === selections.length) return
      let updatedPrompt = prompt.replace(new RegExp(`${escapeRegExp(placeholder)}(?!\\d)`, 'g'), '')
      const normalized = filtered.map((item, idx) => {
        const newPlaceholder = `@selection${idx + 1}`
        if (item.placeholder !== newPlaceholder) {
          const regex = new RegExp(`${escapeRegExp(item.placeholder)}(?!\\d)`, 'g')
          updatedPrompt = updatedPrompt.replace(regex, newPlaceholder)
        }
        return { ...item, placeholder: newPlaceholder }
      })
      setSelections(normalized)
      setPrompt(updatedPrompt)
    },
    [prompt, selections],
  )

  const removeFileMention = useCallback(
    (placeholder: string) => {
      if (!fileMentions.length) return
      const filtered = fileMentions.filter((item) => item.placeholder !== placeholder)
      if (filtered.length === fileMentions.length) return
      let updatedPrompt = prompt.replace(new RegExp(`${escapeRegExp(placeholder)}(?!\\d)`, 'g'), '')
      const normalized = filtered.map((item, idx) => {
        const newPlaceholder = `@file${idx + 1}`
        if (item.placeholder !== newPlaceholder) {
          const regex = new RegExp(`${escapeRegExp(item.placeholder)}(?!\\d)`, 'g')
          updatedPrompt = updatedPrompt.replace(regex, newPlaceholder)
        }
        return { ...item, placeholder: newPlaceholder }
      })
      setFileMentions(normalized)
      setPrompt(updatedPrompt)
    },
    [fileMentions, prompt],
  )

  const clearFileMentionSuggest = useCallback(() => {
    setFileMentionQuery('')
    setFileMentionRange(null)
    setFileMentionActiveIndex(0)
  }, [])

  const addFileMentionFromCandidate = useCallback(
    (filePath: string) => {
      if (!filePath.trim()) return
      const existing = fileMentions.find((item) => item.filePath === filePath)
      let placeholder: string = existing?.placeholder || ''
      if (!placeholder) {
        if (fileMentions.length >= MAX_FILE_MENTION_COUNT) {
          message.warning(`最多可引用 ${MAX_FILE_MENTION_COUNT} 个文件`)
          return
        }
        placeholder = `@file${fileMentions.length + 1}`
        setFileMentions((prev) => [
          ...prev,
          {
            id: `file-mention-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
            filePath,
            placeholder,
          },
        ])
      }
      if (fileMentionRange) {
        setPrompt((prev) => {
          const start = Math.max(0, Math.min(fileMentionRange.start, prev.length))
          const end = Math.max(start, Math.min(fileMentionRange.end, prev.length))
          return `${prev.slice(0, start)}${placeholder}${prev.slice(end)}`
        })
      } else {
        insertPlaceholderAtCursor(placeholder)
      }
      clearFileMentionSuggest()
      requestAnimationFrame(() => {
        promptInputDivRef.current?.focus()
      })
    },
    [clearFileMentionSuggest, fileMentionRange, fileMentions, insertPlaceholderAtCursor],
  )

  const removeComposerMentionToken = useCallback(
    (placeholder: string) => {
      if (placeholder.startsWith('@selection')) {
        removeSelectionSnippet(placeholder)
        return
      }
      if (placeholder.startsWith('@file')) {
        removeFileMention(placeholder)
      }
    },
    [removeFileMention, removeSelectionSnippet],
  )

  // ??prompt ??????????HTML????contentEditable??
  const promptInputDivRef = useRef<HTMLDivElement | null>(null)
  const lastPromptLengthRef = useRef(0)
  
  // ??contentEditable div ?????????? innerText ????????
  const extractTextFromDiv = useCallback((el: HTMLElement): string => {
    let text = ''
    const extract = (node: ChildNode) => {
      if (node.nodeType === Node.TEXT_NODE) {
        text += node.textContent || ''
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        const element = node as HTMLElement
        if (element.classList.contains('doc-studio__prompt-tag')) {
          // ??????????data-placeholder
          text += element.getAttribute('data-placeholder') || ''
        } else if (element.tagName === 'BR') {
          // ????????????
          text += '\n'
        } else if (element.tagName === 'DIV') {
          // div ??????
          if (text && !text.endsWith('\n')) {
            text += '\n'
          }
          element.childNodes.forEach(extract)
        } else {
          // ????????
          element.childNodes.forEach(extract)
        }
      }
    }
    el.childNodes.forEach(extract)
    return text
  }, [])

  useEffect(() => {
    const trailing = extractTrailingFileMentionQuery(prompt)
    if (!trailing) {
      setFileMentionQuery('')
      setFileMentionRange(null)
      setFileMentionActiveIndex(0)
      return
    }
    const rawQuery = String(trailing.query || '')
    if (/^(selection|file)\d+$/i.test(rawQuery)) {
      setFileMentionQuery('')
      setFileMentionRange(null)
      setFileMentionActiveIndex(0)
      return
    }
    setFileMentionQuery(rawQuery)
    setFileMentionRange({ start: trailing.atStart, end: trailing.end })
  }, [prompt])

  useEffect(() => {
    if (!fileMentionDropdownOpen) {
      setFileMentionActiveIndex(0)
      return
    }
    setFileMentionActiveIndex((prev) => Math.max(0, Math.min(prev, fileMentionCandidates.length - 1)))
  }, [fileMentionCandidates.length, fileMentionDropdownOpen])

  useEffect(() => {
    if (!fileMentionDropdownOpen) return
    const handleOutsidePointerDown = (event: MouseEvent | TouchEvent) => {
      const target = event.target as Node | null
      const wrapper = promptWrapperRef.current
      if (!wrapper || !target) return
      if (wrapper.contains(target)) return
      clearFileMentionSuggest()
    }
    document.addEventListener('mousedown', handleOutsidePointerDown, true)
    document.addEventListener('touchstart', handleOutsidePointerDown, true)
    return () => {
      document.removeEventListener('mousedown', handleOutsidePointerDown, true)
      document.removeEventListener('touchstart', handleOutsidePointerDown, true)
    }
  }, [clearFileMentionSuggest, fileMentionDropdownOpen])
  
  useEffect(() => {
    const el = promptInputDivRef.current
    if (!el) return
    
    // ??????????????????????
    const currentText = extractTextFromDiv(el)
    if (currentText === prompt) return
    
    // ????????????????????
    const isAppending = prompt.length > lastPromptLengthRef.current
    lastPromptLengthRef.current = prompt.length
    
    // ??HTML
    let text = prompt
    if (!text) {
      el.innerHTML = ''
      return
    }
    
    // ?? HTML???????????????????
    const placeholderPattern = new RegExp(COMPOSER_PLACEHOLDER_REGEX.source, 'g')
    const placeholders: { match: string; index: number }[] = []
    let match: RegExpExecArray | null
    
    while ((match = placeholderPattern.exec(text)) !== null) {
      placeholders.push({ match: match[0], index: match.index })
    }
    
    // ???? HTML
    let html = ''
    let lastIndex = 0
    
    placeholders.forEach(({ match, index }) => {
      // ???????????????????????
      if (index > lastIndex) {
        const plainText = text.slice(lastIndex, index)
        const escapedText = plainText
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
          .replace(/\n/g, '<br>')
        html += escapedText
      }
      
      // ????????????????
      const tagClass = match.startsWith('@file')
        ? 'doc-studio__prompt-tag doc-studio__prompt-tag--file'
        : 'doc-studio__prompt-tag'
      html += `<span class="${tagClass}" contenteditable="false" data-placeholder="${match}"><span class="anticon anticon-file-text"><svg viewBox="64 64 896 896" focusable="false" width="10" height="10" fill="currentColor"><path d="M854.6 288.6L639.4 73.4c-6-6-14.1-9.4-22.6-9.4H192c-17.7 0-32 14.3-32 32v832c0 17.7 14.3 32 32 32h640c17.7 0 32-14.3 32-32V311.3c0-8.5-3.4-16.7-9.4-22.7zM790.2 326H602V137.8L790.2 326zm1.8 562H232V136h302v216a42 42 0 0042 42h216v494z"></path></svg></span><span>${match}</span><span class="anticon anticon-close prompt-tag-close" data-action="remove-${match}"><svg viewBox="64 64 896 896" focusable="false" width="9" height="9" fill="currentColor"><path d="M563.8 512l262.5-312.9c4.4-5.2.7-13.1-6.1-13.1h-79.8c-4.7 0-9.2 2.1-12.3 5.7L511.6 449.8 295.1 191.7c-3-3.6-7.5-5.7-12.3-5.7H203c-6.8 0-10.5 7.9-6.1 13.1L459.4 512 196.9 824.9A7.95 7.95 0 00203 838h79.8c4.7 0 9.2-2.1 12.3-5.7l216.5-258.1 216.5 258.1c3 3.6 7.5 5.7 12.3 5.7h79.8c6.8 0 10.5-7.9 6.1-13.1L563.8 512z"></path></svg></span></span>`
      
      lastIndex = index + match.length
    })
    
    // ?????????
    if (lastIndex < text.length) {
      const plainText = text.slice(lastIndex)
      const escapedText = plainText
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\n/g, '<br>')
      html += escapedText
    }
    
    el.innerHTML = html
    
    // ????????????????????????
    if (isAppending) {
      requestAnimationFrame(() => {
        try {
          const selection = window.getSelection()
          if (selection && el.childNodes.length > 0) {
            const range = document.createRange()
            // ?????????
            const lastChild = el.childNodes[el.childNodes.length - 1]
            
            if (lastChild.nodeType === Node.TEXT_NODE) {
              // ?????????????????????
              range.setStart(lastChild, (lastChild as Text).length)
            } else {
              // ???????????????????????????
              range.setStartAfter(lastChild)
            }
            
            range.collapse(true)
            selection.removeAllRanges()
            selection.addRange(range)
          }
        } catch (e) {
          // ????????
        }
      })
    }
  }, [fileMentions, prompt, selections])

  const currentFileBuffer = snap.activeFilePath
    ? snap.files[snap.activeFilePath]
    : undefined

  const agentWarnings = snap.agentStatus.warnings ?? []

  const llmMetricEntries = useMemo(() => {
    const metrics = metricsSummary?.llm || {}
    return Object.entries(metrics).map(([key, value]) => {
      const parts = key.split('::')
      const provider = parts[0] || key
      const model = parts.slice(1).join('::') || key
      return {
        key,
        provider,
        model,
        ...value,
      }
    })
  }, [metricsSummary])

  const llmTotals = useMemo(() => {
    let tokens = 0
    let cost = 0
    llmMetricEntries.forEach((entry) => {
      tokens += entry.total_tokens || 0
      cost += entry.total_cost || 0
    })
    return { tokens, cost }
  }, [llmMetricEntries])

  const activeFileTimeline = useMemo(() => {
    if (!snap.activeFilePath) return []
    const normalizedActivePath = normalizeWorkspacePath(snap.activeFilePath)
    const list = operationHistory.filter((item) =>
      Array.isArray(item.modified_files) &&
      item.modified_files.some(
        (filePath) => normalizeWorkspacePath(String(filePath || '')) === normalizedActivePath,
      ),
    )
    return [...list].sort((a, b) => {
      const left = Date.parse(a.timestamp || '')
      const right = Date.parse(b.timestamp || '')
      if (!Number.isNaN(left) && !Number.isNaN(right)) return right - left
      if (!Number.isNaN(right)) return 1
      if (!Number.isNaN(left)) return -1
      return b.operation_id.localeCompare(a.operation_id)
    })
  }, [operationHistory, snap.activeFilePath])

  const openFile = useCallback(
    async (path: string, forceReload = false, silent = false) => {
      if (!docStudioState.workspaceId) return
      if (!forceReload) {
        const existing = docStudioState.files[path]
        if (existing && !existing.loading) {
          docStudioActions.setActiveFile(path)
          return
        }
      }
      docStudioActions.setActiveFile(path)
      if (!silent) {
      docStudioActions.setFileLoading(path, true)
      }
      try {
        const file = await fetchFileContent({
          workspaceId: docStudioState.workspaceId,
          path,
        }, {
          loading: false,
          errorToast: false,
        })
        docStudioActions.setFileContent(path, file.content, file.encoding)
      } catch (error) {
        showRequestError(error)
      } finally {
        docStudioActions.setFileLoading(path, false)
      }
    },
    [showRequestError],
  )

  /**
   * M12 论点-论据联动：chat 回答里的 "5-3 场" → 切换到对应场景。
   * 解析失败 fail aloud（toast 警告但不抛异常，避免 UI 崩）。
   */
  const handleSceneRefJump = useCallback(
    (ref: string) => {
      const workspaceId = docStudioState.workspaceId
      if (!workspaceId) {
        message.warning('请先选择剧本工作区')
        return
      }
      const hit = findSceneByRef(workspaceId, ref)
      if (!hit) {
        message.warning(`未找到场景 "${ref}"，可能是 LLM 引用了不存在的集场`)
        return
      }
      void openFile(hit.id, false, false)
    },
    [openFile],
  )

  // 当前激活场景在文件树里的可读名（用于 Modal 顶部展示）
  const activeSceneLabel = useMemo(() => {
    if (!snap.activeFilePath) return ''
    const find = (nodes?: typeof snap.fileTree): string | null => {
      if (!nodes) return null
      for (const n of nodes) {
        if (n.type === 'file' && n.path === snap.activeFilePath) return n.name
        if (n.type === 'directory') {
          const r = find(n.children)
          if (r) return r
        }
      }
      return null
    }
    return find(snap.fileTree) || snap.activeFilePath
  }, [snap.activeFilePath, snap.fileTree])

  /**
   * M3 场景改写：调 POST /rewrite → 注入 AgentDiffReview state pipeline。
   * 复用 ScholarMind in-place toggle，AgentDiffReview 直接展示 unified diff。
   */
  const handleSubmitRewrite = useCallback(async () => {
    const workspaceId = docStudioState.workspaceId
    const sceneId = docStudioState.activeFilePath
    if (!workspaceId) {
      message.warning('请先选择剧本')
      return
    }
    if (!sceneId) {
      message.warning('请先在左侧选择一个场景')
      return
    }
    const issue = rewriteIssue.trim()
    if (!issue) {
      message.warning('请描述本场需要改进的问题')
      return
    }
    setRewriteSubmitting(true)
    try {
      const resp = await rewriteScript(workspaceId, {
        scene_id: sceneId,
        target_dimension: rewriteDimension,
        issue,
      })
      // 注入 ScholarMind diff 复用管线，触发中央 pane in-place AgentDiffReview
      setAllFileDiffs([
        {
          file_path: sceneId,
          original_content: resp.original_text || '',
          modified_content: resp.rewritten_text || '',
        },
      ])
      setCurrentDiffIndex(0)
      setAgentDiffReviewOpen(true)
      setDiffModalContext('agent')
      setDiffModalOpen(false)
      setRewriteModalOpen(false)
      setRewriteIssue('')
      message.success(
        resp.rationale
          ? `改写完成：${resp.rationale.slice(0, 60)}${resp.rationale.length > 60 ? '...' : ''}`
          : '改写完成，请在中央 diff 视图比对',
      )
    } catch (err: unknown) {
      const e = err as {
        response?: { data?: { detail?: string }; status?: number }
        message?: string
      }
      message.error(`改写失败：${e?.response?.data?.detail || e?.message || '未知错误'}`)
    } finally {
      setRewriteSubmitting(false)
    }
  }, [rewriteDimension, rewriteIssue])

  const revealEditorSelectionByOffset = useCallback((startOffset: number, endOffset: number) => {
    const editor = editorRef.current
    const model = editor?.getModel?.()
    if (!editor || !model) return false
    const totalLength = Number(model.getValueLength?.() ?? model.getValue?.().length ?? 0)
    if (!Number.isFinite(totalLength) || totalLength < 0) return false
    const safeStart = Math.max(0, Math.min(Math.floor(startOffset), totalLength))
    const safeEnd = Math.max(safeStart, Math.min(Math.floor(endOffset), totalLength))
    const startPos = model.getPositionAt(safeStart)
    const endPos = model.getPositionAt(safeEnd)
    if (!startPos || !endPos) return false
    const monaco = typeof window !== 'undefined' ? (window as any).monaco : undefined
    const range = monaco?.Range
      ? new monaco.Range(
          startPos.lineNumber,
          startPos.column,
          endPos.lineNumber,
          endPos.column,
        )
      : {
          startLineNumber: startPos.lineNumber,
          startColumn: startPos.column,
          endLineNumber: endPos.lineNumber,
          endColumn: endPos.column,
        }
    editor.setSelection(range)
    if (typeof editor.revealRangeInCenter === 'function') {
      editor.revealRangeInCenter(range)
    } else if (typeof editor.revealLineInCenter === 'function') {
      editor.revealLineInCenter(startPos.lineNumber)
    }
    editor.focus()
    return true
  }, [])

  const handleMentionTagClick = useCallback(
    async (target: MentionTagClickTarget) => {
      const filePath = String(target.filePath || '').trim()
      if (!filePath) {
        message.warning('引用缺少文件路径，无法跳转')
        return
      }
      await openFile(filePath, false, true)
      if (target.type !== 'selection') return
      const start = Number(target.start)
      const end = Number(target.end)
      if (!Number.isFinite(start) || start < 0) {
        message.warning('该选区缺少位置信息，已为你打开文件')
        return
      }
      const selectionEnd = Number.isFinite(end) && end >= start ? end : start
      let positioned = false
      for (let attempt = 0; attempt < 8; attempt += 1) {
        if (revealEditorSelectionByOffset(start, selectionEnd)) {
          positioned = true
          break
        }
        // eslint-disable-next-line no-await-in-loop
        await new Promise((resolve) => window.setTimeout(resolve, 50))
      }
      if (!positioned) {
        message.warning('已打开文件，但暂时无法定位到该选区')
      }
    },
    [openFile, revealEditorSelectionByOffset],
  )

  const loadWorkspaceChatHistory = useCallback(
    async (_workspaceId: string, config: Record<string, any>, sessionIdOverride?: string | null) => {
      const sessionId = sessionIdOverride ?? config?.session_id ?? config?.sessionId
      if (!sessionId) {
        docStudioActions.setChatMessages([])
        return
      }
      try {
        const data = await listWorkspaceMessages({
          workspaceId: _workspaceId,
          sessionId: String(sessionId),
          page: 1,
          pageSize: 200,
        }, {
          loading: false,
          errorToast: false,
        })
        const items = Array.isArray(data?.items) ? data.items : []
        const messages: DocStudioChatMessage[] = []
        items.forEach((item) => {
          const retrievalData = parseRetrievalContent(item.retrieval_content)
          const historyImagesRaw = Array.isArray(retrievalData?.images)
            ? retrievalData.images
            : Array.isArray(retrievalData?.image_attachments)
              ? retrievalData.image_attachments
              : Array.isArray(retrievalData?.imageAttachments)
                ? retrievalData.imageAttachments
                : []
          const historyImages: ChatImageAttachment[] = historyImagesRaw
            .map((img: any, idx: number) => {
              const dataUrl =
                typeof img?.dataUrl === 'string'
                  ? img.dataUrl
                  : typeof img?.data_url === 'string'
                    ? img.data_url
                    : ''
              if (!dataUrl) return null
              const sizeRaw = Number(img?.size || 0)
              return {
                id: String(img?.id || `${item.message_id || 'history'}-img-${idx + 1}`),
                name: String(img?.name || `image-${idx + 1}`),
                mimeType: String(img?.mimeType || img?.mime_type || 'image/png'),
                size: Number.isFinite(sizeRaw) && sizeRaw > 0 ? sizeRaw : 0,
                dataUrl,
              }
            })
            .filter((img: ChatImageAttachment | null): img is ChatImageAttachment => Boolean(img))
          const historySelections = normalizeSelectionFragments(
            retrievalData?.selections
              ?? retrievalData?.selection_fragments
              ?? retrievalData?.selectionFragments
              ?? retrievalData?.selection,
          )
          const historyFileMentions = normalizeFileMentionFragments(
            retrievalData?.file_mentions
              ?? retrievalData?.fileMentions
              ?? retrievalData?.files,
          )
          const createdAt = item.create_time ? Date.parse(item.create_time) : Date.now()
          const baseId = item.message_id || `${createdAt}-${Math.random()}`
          if (item.user_question) {
            messages.push({
              id: `${baseId}-user`,
              role: 'user',
              content: item.user_question,
              createdAt,
              meta: {
                messageId: item.message_id,
                source: retrievalData?.source,
                workspaceId: retrievalData?.workspace_id,
                traceId: retrievalData?.trace_id,
                runId: retrievalData?.run_id || retrievalData?.runId,
                imageCount: historyImages.length || 0,
                images: historyImages,
                selectionCount: historySelections.length || 0,
                selections: historySelections,
                fileMentionCount: historyFileMentions.length || 0,
                fileMentions: historyFileMentions,
              },
            })
          }
          if (item.model_answer) {
            messages.push({
              id: `${baseId}-agent`,
              role: 'agent',
              content: item.model_answer,
              createdAt: createdAt + 1,
              meta: {
                messageId: item.message_id,
                source: retrievalData?.source,
                workspaceId: retrievalData?.workspace_id,
                traceId: retrievalData?.trace_id,
                runId: retrievalData?.run_id || retrievalData?.runId,
              },
            })
          }
        })
        docStudioActions.setChatMessages(messages)
        const sid = String(sessionId)
        const firstUserPrompt = messages.find((msg) => msg.role === 'user')?.content
        const autoTitle = buildSessionTitleFromPrompt(firstUserPrompt)
        const currentTitle = String(sessionTitlesRef.current[sid] || '').trim()
        if (autoTitle && (!currentTitle || isPlaceholderSessionTitle(currentTitle))) {
          sessionTitlesRef.current = {
            ...sessionTitlesRef.current,
            [sid]: autoTitle,
          }
          setSessionTitleVersion((value) => value + 1)
          if (!autoTitledSessionRef.current[sid]) {
            autoTitledSessionRef.current[sid] = true
            void renameSession(
              { sessionId: sid, sessionName: autoTitle },
              { loading: false, errorToast: false },
            ).catch(() => {})
          }
        }
      } catch (error) {
        console.warn('[DocStudio] 加载对话历史失败:', error)
        docStudioActions.setChatMessages([])
      }
    },
    [showRequestError],
  )

  const isRestoringWorkspaceRef = useRef(false)

  const loadWorkspaceFiles = useCallback(
    async (workspaceId: string, shouldOpenDefault = true) => {
      try {
        isRestoringWorkspaceRef.current = true
        const data = await fetchWorkspaceFiles({ workspaceId }, {
          loading: false,
          errorToast: false,
        })
        docStudioActions.setFileTree(data.files)
        docStudioActions.setWorkspaceConfig(data.config)
        applyLlmOptionsFromConfig(data.config)
        // Cursor 逻辑：有 session_id 则加载持久化对话；无则显示「新对话」
        await loadWorkspaceChatHistory(workspaceId, data.config)
        setLlmOptionsReady(true)
        
        // 打印文件树（用于调试）
        const printFileTree = (nodes: any[], indent = '') => {
          for (const node of nodes) {
            console.log(
              `${indent}${node.type === 'directory' ? '目录' : '文件'} ${node.name} (${node.path})`,
            )
            if (node.children && node.children.length > 0) {
              printFileTree(node.children, indent + '  ')
            }
          }
        }
        console.log('当前文件树:')
        printFileTree(data.files)

        // 从 localStorage 恢复上次打开的文件
        if (shouldOpenDefault) {
          const allPaths = collectAllFilePaths(data.files)
          const preferredFile =
            preferredFileFromUrl && allPaths.includes(preferredFileFromUrl)
              ? preferredFileFromUrl
              : ''
          const storageKey = `latex_editor_workspace_state_${workspaceId}`
          let restored = false
          try {
            if (preferredFile) {
              await openFile(preferredFile, true)
              docStudioActions.setActiveFile(preferredFile)
              restored = true
            }
            const raw = localStorage.getItem(storageKey)
            if (raw && !restored) {
              const parsed = JSON.parse(raw) as {
                openedFiles?: string[]
                activeFilePath?: string
              }
              const validOpened = parsed.openedFiles?.filter((p) => allPaths.includes(p)) ?? []

              if (validOpened.length > 0) {
                // ?????????????
                for (const path of validOpened) {
                  // eslint-disable-next-line no-await-in-loop
                  await openFile(path)
                }
                // 恢复上次激活的文件
                if (parsed.activeFilePath && validOpened.includes(parsed.activeFilePath)) {
                  docStudioActions.setActiveFile(parsed.activeFilePath)
                }
                restored = true
              }
            }
          } catch (e) {
            // eslint-disable-next-line no-console
            console.warn('恢复工作区状态失败', e)
          }

          if (!restored) {
            docStudioActions.setActiveFile('')
          }
        }
      } catch (error) {
        showRequestError(error)
      } finally {
        isRestoringWorkspaceRef.current = false
      }
    },
    [
      applyLlmOptionsFromConfig,
      openFile,
      loadWorkspaceChatHistory,
      preferredFileFromUrl,
    ],
  )

  const syncWorkspaceFileTree = useCallback(
    async (workspaceId: string) => {
      if (!workspaceId) return
      try {
        const data = await fetchWorkspaceFiles(
          { workspaceId },
          {
            loading: false,
            errorToast: false,
          },
        )
        docStudioActions.setFileTree(data.files)
      } catch (error) {
        console.warn('[DocStudio] 静默刷新文件树失败', error)
      }
    },
    [],
  )

  const loadKnowledgeBases = useCallback(async () => {
    setKnowledgeLoading(true)
    try {
      const data = await listAgentKnowledgeBases({
        loading: false,
        errorToast: false,
      })
      const list = Array.isArray(data) ? data : []
      // ?????????ephemeral??????????
      const permanentBases = list.filter((item) => !item.is_ephemeral)
      setKnowledgeBases(permanentBases)
      const nextKbId = resolvePreferredKnowledgeBaseId(permanentBases, [
        selectedKnowledgeBaseIdRef.current,
        preferredKbFromUrl,
      ])
      setSelectedKnowledgeBaseId(nextKbId)
      if (nextKbId != null) {
        persistLastUsedKnowledgeBaseId(nextKbId)
      }
    } catch (error) {
      showRequestError(error)
      if (asyncMode) {
        setChatLoading(false)
      }
    } finally {
      setKnowledgeLoading(false)
    }
  }, [preferredKbFromUrl])

  useEffect(() => {
    selectedKnowledgeBaseIdRef.current = selectedKnowledgeBaseId
  }, [selectedKnowledgeBaseId])

  const chatSessionIds = useMemo(() => {
    const ids = snap.workspaceConfig?.session_ids
    const list = Array.isArray(ids) ? ids : []
    return list.filter((id): id is string => id !== '__new__')
  }, [snap.workspaceConfig?.session_ids])

  const hasNewConversationSlot = useMemo(() => {
    const ids = snap.workspaceConfig?.session_ids
    return Array.isArray(ids) && ids.includes('__new__')
  }, [snap.workspaceConfig?.session_ids])

  const closedSessionIds = useMemo(() => {
    const ids = snap.workspaceConfig?.session_history
    return Array.isArray(ids) ? ids : []
  }, [snap.workspaceConfig?.session_history])

  const currentChatSessionId = snap.workspaceConfig?.session_id ?? snap.workspaceConfig?.sessionId ?? null

  const isPlaceholderSessionTitle = useCallback((value?: string) => {
    const text = String(value || '').trim()
    if (!text) return true
    if (/^session[_\s-]/i.test(text)) return true
    if (/^session kb \d+$/i.test(text)) return true
    if (/^session for kb \d+$/i.test(text)) return true
    if (/^message-only session /i.test(text)) return true
    if (/^对话 [a-z0-9_-]{4,}$/i.test(text)) return true
    return false
  }, [])

  const buildSessionTitleFromPrompt = useCallback((rawPrompt?: string) => {
    const normalized = String(rawPrompt || '')
      .replace(/\[已附带图片\s*\d+\s*张\]/g, ' ')
      .replace(/@selection\d+/gi, ' ')
      .replace(/@file\d+/gi, ' ')
      .replace(/`+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
    if (!normalized) return ''

    const politePrefixRegex = /^(请你|请帮我|请|帮我|麻烦你|麻烦|可以帮我|可以|能不能|能否|我想|我需要|我希望)\s*/i
    const weakStartRegex = /^(这里|这段|这个|当前|现在)\s*/i
    const actionHints = [
      '修改',
      '改写',
      '重写',
      '润色',
      '优化',
      '修复',
      '排查',
      '分析',
      '总结',
      '解释',
      '完善',
      '重构',
      '补充',
      '新增',
      '删除',
      '调整',
      'replace',
      'rewrite',
      'refactor',
      'fix',
      'summarize',
      'analyze',
      'optimize',
    ]

    const sanitizeSegment = (segment: string) =>
      segment
        .replace(politePrefixRegex, '')
        .replace(weakStartRegex, '')
        .replace(/\s+/g, ' ')
        .trim()

    const segments = normalized
      .split(/[。！？!?；;：:\n]/)
      .map((item) => sanitizeSegment(item))
      .filter(Boolean)

    let candidate = segments.find((item) =>
      actionHints.some((hint) => item.toLowerCase().includes(hint.toLowerCase())),
    ) || segments[0] || normalized

    if (candidate.length < 10 && segments.length > 1) {
      candidate = `${candidate} · ${segments[1]}`
    }

    candidate = candidate
      .replace(/^[，,。.；;：:\-\s]+/, '')
      .replace(/[，,。.；;：:\-\s]+$/, '')
      .trim()

    if (!candidate) candidate = normalized

    const softLimit = 36
    const hardLimit = 120
    if (candidate.length > hardLimit) {
      return `${candidate.slice(0, hardLimit).trim()}...`
    }
    if (candidate.length > softLimit) {
      return `${candidate.slice(0, softLimit).trim()}...`
    }
    return candidate
  }, [])

  useEffect(() => {
    if (!historyDropdownOpen && historySearchKeyword) {
      setHistorySearchKeyword('')
    }
  }, [historyDropdownOpen, historySearchKeyword])

  useEffect(() => {
    sessionTitlesRef.current = {}
    autoTitledSessionRef.current = {}
  }, [snap.workspaceId])

  useEffect(() => {
    if (!snap.workspaceId) return
    const allSessionIds = Array.from(new Set([...chatSessionIds, ...closedSessionIds].filter(Boolean)))
    if (!allSessionIds.length) return
    let cancelled = false
    ;(async () => {
      try {
        const { data } = await listSessions(
          { surface: 'doc_studio' },
          { loading: false, errorToast: false },
        )
        if (cancelled) return
        const rows = Array.isArray(data?.sessions) ? data.sessions : []
        const sessionNameMap = new Map(
          rows.map((item) => [String(item.session_id || ''), String(item.session_name || '').trim()]),
        )

        const syncedTitles: Record<string, string> = {}
        allSessionIds.forEach((sid) => {
          const storedTitle = String(sessionNameMap.get(sid) || '').trim()
          if (storedTitle && sessionTitlesRef.current[sid] !== storedTitle) {
            syncedTitles[sid] = storedTitle
          }
        })
        if (Object.keys(syncedTitles).length > 0) {
          sessionTitlesRef.current = {
            ...sessionTitlesRef.current,
            ...syncedTitles,
          }
          setSessionTitleVersion((value) => value + 1)
        }

        const autoPatchedTitles: Record<string, string> = {}
        for (const sid of allSessionIds) {
          if (cancelled) return
          if (!sessionNameMap.has(sid)) continue
          if (autoTitledSessionRef.current[sid]) continue
          const knownTitle = String(
            autoPatchedTitles[sid] || syncedTitles[sid] || sessionTitlesRef.current[sid] || '',
          ).trim()
          if (knownTitle && !isPlaceholderSessionTitle(knownTitle)) continue
          try {
            const { data: historyData } = await listSessionMessages(
              {
                sessionId: sid,
                page: 1,
                pageSize: 20,
              },
              { loading: false, errorToast: false },
            )
            if (cancelled) return
            const firstPrompt = (Array.isArray(historyData?.items) ? historyData.items : []).find(
              (item) => typeof item.user_question === 'string' && item.user_question.trim(),
            )?.user_question
            const autoTitle = buildSessionTitleFromPrompt(firstPrompt)
            if (!autoTitle) continue
            autoPatchedTitles[sid] = autoTitle
            autoTitledSessionRef.current[sid] = true
            void renameSession(
              { sessionId: sid, sessionName: autoTitle },
              { loading: false, errorToast: false },
            ).catch(() => {})
          } catch {
            // 忽略单个会话标题补齐失败
          }
        }
        if (!cancelled && Object.keys(autoPatchedTitles).length > 0) {
          sessionTitlesRef.current = {
            ...sessionTitlesRef.current,
            ...autoPatchedTitles,
          }
          setSessionTitleVersion((value) => value + 1)
        }
      } catch {
        // 忽略会话标题拉取失败，不影响主流程
      }
    })()
    return () => {
      cancelled = true
    }
  }, [snap.workspaceId, chatSessionIds, closedSessionIds, isPlaceholderSessionTitle, buildSessionTitleFromPrompt])

  // 当处于新对话状态且 session_ids 中无 __new__ 占位符时，调用 bind(null) 以插入占位符，便于后续「替换」逻辑
  // 注意：仅在已有其他会话时插入 __new__，避免与「首次发送」竞态（用户发送时 create+bind 会插入新会话，若 bind(null) 晚返回会覆盖为 [__new__, newSessionId]）
  useEffect(() => {
    if (!snap.workspaceId || currentChatSessionId) return
    if (hasNewConversationSlot) return
    if (chatSessionIds.length === 0) return
    bindWorkspaceSession(
      { workspaceId: snap.workspaceId, sessionId: null },
      { loading: false, errorToast: false },
    )
      .then((res) => {
        const cur = docStudioState.workspaceConfig
        const curSessionId = cur?.session_id ?? cur?.sessionId
        if (curSessionId) return
        docStudioActions.setWorkspaceConfig(res.config)
      })
      .catch(() => {})
  }, [snap.workspaceId, currentChatSessionId, hasNewConversationSlot, chatSessionIds.length])

  /** Cursor 风格：点击 + 新建对话 → 切换到「新对话」占位，不立即创建 session，等用户发送首条消息时再创建 */
  const handleNewChat = useCallback(async () => {
    if (!snap.workspaceId) return
    try {
      docStudioActions.setChatMessages([])
      const detail = await bindWorkspaceSession(
        { workspaceId: snap.workspaceId, sessionId: null },
        { loading: false, errorToast: false },
      )
      docStudioActions.setWorkspaceConfig(detail.config)
    } catch {
      docStudioActions.setWorkspaceConfig({ ...snap.workspaceConfig, session_id: null })
    }
  }, [snap.workspaceId, snap.workspaceConfig])

  const handleSwitchChatSession = useCallback(
    async (sessionId: string) => {
      if (!snap.workspaceId) return
      try {
        await bindWorkspaceSession(
          { workspaceId: snap.workspaceId, sessionId },
          { loading: false, errorToast: false },
        )
        const data = await fetchWorkspaceFiles({ workspaceId: snap.workspaceId }, {
          loading: false,
          errorToast: false,
        })
        docStudioActions.setWorkspaceConfig(data.config)
        await loadWorkspaceChatHistory(snap.workspaceId, data.config, sessionId)
      } catch (e) {
        showRequestError(e)
      }
    },
    [snap.workspaceId, loadWorkspaceChatHistory],
  )

  /** 关闭 tab：仅从打开列表移除，加入历史记录，可随时从历史重新打开 */
  const handleCloseChatSession = useCallback(
    async (sessionId: string) => {
      if (!snap.workspaceId) return
      const ids = chatSessionIds.filter((id) => id !== sessionId)
      const nextCurrent = ids[0] ?? null
      const idsForConfig = nextCurrent ? ids : [...ids, '__new__']
      const history = [...closedSessionIds]
      if (!history.includes(sessionId)) {
        history.unshift(sessionId)
      }
      const newConfig = {
        ...snap.workspaceConfig,
        session_ids: idsForConfig,
        session_id: nextCurrent,
        session_history: history,
      }
      try {
        await updateWorkspace(
          { workspaceId: snap.workspaceId, config: newConfig },
          { loading: false, errorToast: false },
        )
        docStudioActions.setWorkspaceConfig(newConfig)
        if (currentChatSessionId === sessionId) {
          if (nextCurrent) {
            await loadWorkspaceChatHistory(snap.workspaceId, newConfig, nextCurrent)
          } else {
            docStudioActions.setChatMessages([])
          }
        }
      } catch (e) {
        showRequestError(e)
      }
    },
    [
      snap.workspaceId,
      snap.workspaceConfig,
      chatSessionIds,
      closedSessionIds,
      currentChatSessionId,
      loadWorkspaceChatHistory,
    ],
  )

  /** 从历史记录重新打开对话 */
  const handleReopenChatSession = useCallback(
    async (sessionId: string) => {
      if (!snap.workspaceId) return
      let ids = chatSessionIds.includes(sessionId) ? chatSessionIds : [sessionId, ...chatSessionIds]
      if (!ids.includes('__new__')) ids = [...ids, '__new__']
      const history = closedSessionIds.filter((id) => id !== sessionId)
      const newConfig = {
        ...snap.workspaceConfig,
        session_ids: ids,
        session_id: sessionId,
        session_history: history,
      }
      try {
        await updateWorkspace(
          { workspaceId: snap.workspaceId, config: newConfig },
          { loading: false, errorToast: false },
        )
        docStudioActions.setWorkspaceConfig(newConfig)
        await loadWorkspaceChatHistory(snap.workspaceId, newConfig, sessionId)
      } catch (e) {
        showRequestError(e)
      }
    },
    [snap.workspaceId, snap.workspaceConfig, chatSessionIds, closedSessionIds, loadWorkspaceChatHistory],
  )

  /** 彻底删除：删除后端数据，从打开列表和历史中移除，不可恢复 */
  const handleDeleteChatSession = useCallback(
    async (sessionId: string) => {
      if (!snap.workspaceId) return
      try {
        try {
          await removeSession({ sessionId }, { loading: false, errorToast: false })
        } catch (e: any) {
          const status = e?.response?.status
          if (status !== 404) {
            throw e
          }
          // 历史残留会话：后端已不存在，允许继续做本地配置清理。
          console.warn('[DocStudio] 删除会话时发现后端不存在，继续清理本地引用', {
            sessionId,
            detail: e?.response?.data?.detail,
          })
        }
        const ids = chatSessionIds.filter((id) => id !== sessionId)
        const history = closedSessionIds.filter((id) => id !== sessionId)
        const nextCurrent = ids[0] ?? null
        const idsForConfig = nextCurrent ? ids : [...ids, '__new__']
        const newConfig = {
          ...snap.workspaceConfig,
          session_ids: idsForConfig,
          session_id: nextCurrent,
          session_history: history,
        }
        await updateWorkspace(
          { workspaceId: snap.workspaceId, config: newConfig },
          { loading: false, errorToast: false },
        )
        docStudioActions.setWorkspaceConfig(newConfig)
        if (currentChatSessionId === sessionId) {
          if (nextCurrent) {
            await loadWorkspaceChatHistory(snap.workspaceId, newConfig, nextCurrent)
          } else {
            docStudioActions.setChatMessages([])
          }
        }
      } catch (e) {
        showRequestError(e)
      }
    },
    [
      snap.workspaceId,
      snap.workspaceConfig,
      chatSessionIds,
      closedSessionIds,
      currentChatSessionId,
      loadWorkspaceChatHistory,
    ],
  )

  const handleRenameChatSession = useCallback(
    (sessionId: string, initialTitle?: string) => {
      setHistoryDropdownOpen(false)
      const fallbackTitle = String(sessionTitlesRef.current[sessionId] || '').trim()
      const presetValue = String(initialTitle || fallbackTitle || '').trim()
      let draftName = presetValue
      Modal.confirm({
        title: '重命名对话',
        okText: '保存',
        cancelText: '取消',
        closable: true,
        maskClosable: true,
        content: (
          <Input
            autoFocus
            maxLength={120}
            defaultValue={presetValue}
            placeholder="请输入对话名称"
            onChange={(event) => {
              draftName = event.target.value
            }}
          />
        ),
        onOk: async () => {
          const nextName = String(draftName || '').trim()
          if (!nextName) {
            message.warning('会话名称不能为空')
            throw new Error('__invalid_session_name__')
          }
          try {
            const { data } = await renameSession(
              { sessionId, sessionName: nextName },
              { loading: false, errorToast: false },
            )
            const finalName = String(data?.sessionName || nextName).trim() || nextName
            sessionTitlesRef.current = {
              ...sessionTitlesRef.current,
              [sessionId]: finalName,
            }
            autoTitledSessionRef.current[sessionId] = true
            setSessionTitleVersion((value) => value + 1)
            setHistoryDropdownOpen(false)
            message.success('对话名称已更新')
          } catch (error) {
            showRequestError(error)
            throw error
          }
        },
      })
    },
    [showRequestError],
  )

  const loadWorkspaces = useCallback(
    async (targetWorkspace?: string) => {
      docStudioActions.setWorkspaceLoading(true)
      try {
        const list = await listWorkspaces({
          loading: false,
          errorToast: false,
        })
        docStudioActions.setWorkspaces(list)
        // 兜底候选必须排除 Notebook：Notebook 只能通过 /doc-studio/notebook 显式访问，
        // 否则用户先打开 Notebook 再切到 /doc-studio 时，会把 Notebook 误装进 Doc Studio 视图。
        const explicit = targetWorkspace || params.workspaceId || ''
        const fallback =
          list.find((item) => item.workspaceId !== NOTEBOOK_WORKSPACE_ID)?.workspaceId || ''
        const preferred = explicit || fallback
        if (preferred) {
          docStudioActions.setWorkspaceId(preferred)
          await loadWorkspaceFiles(preferred)
        } else {
          // 没有任何 Doc Studio 工作区时，明确清空状态，进入 Doc Studio 空白态，
          // 避免从 /doc-studio/notebook 切回 /doc-studio 时残留 Notebook 的文件树和会话。
          docStudioActions.setWorkspaceId('')
        }
      } catch (error) {
        showRequestError(error)
      } finally {
        docStudioActions.setWorkspaceLoading(false)
      }
    },
    [loadWorkspaceFiles, params.workspaceId],
  )

  useEffect(() => {
    loadWorkspaces(params.workspaceId)
  }, [loadWorkspaces, params.workspaceId])

  useEffect(() => {
    loadKnowledgeBases()
  }, [loadKnowledgeBases])

  useEffect(() => {
    if (!supportsCompilePanel && rightTab === 'compile') {
      setRightTab('chat')
    }
  }, [supportsCompilePanel, rightTab])

  useEffect(() => {
    try {
      localStorage.setItem('doc_studio_right_tab', rightTab)
    } catch {
      // ignore
    }
  }, [rightTab])

  useEffect(() => {
    const el = chatInputContainerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver((entries) => {
      const { width } = entries[0]?.contentRect ?? {}
      setChatToolbarCompact(typeof width === 'number' && width < 340)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [rightTab])

  // 持久化工作区状态到 localStorage（恢复过程中不覆盖，避免 setWorkspaceId 清空后立即写入空数据）
  useEffect(() => {
    if (!snap.workspaceId || isRestoringWorkspaceRef.current) return
    const storageKey = `latex_editor_workspace_state_${snap.workspaceId}`
    const payload = {
      openedFiles: snap.openedFiles,
      activeFilePath: snap.activeFilePath,
    }
    try {
      localStorage.setItem(storageKey, JSON.stringify(payload))
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn('保存工作区状态失败', e)
    }
  }, [snap.workspaceId, snap.openedFiles, snap.activeFilePath])

  // ??????workspaceConfig ????????
  // ?????????????????????????
  // useEffect(() => {
  //   const workspaceKbRaw =
  //     (snap.workspaceConfig?.knowledge_base_id ??
  //       snap.workspaceConfig?.knowledgeBaseId ??
  //       snap.workspaceConfig?.kb_id) as number | undefined
  //   if (!workspaceKbRaw) return
  //   setSelectedKnowledgeBaseId((current) => current || Number(workspaceKbRaw))
  // }, [snap.workspaceConfig])

  const lastChatMessageId =
    snap.chatMessages.length > 0
      ? snap.chatMessages[snap.chatMessages.length - 1]?.id ?? null
      : null

  // ????????????????????????????
  useEffect(() => {
    if (!lastChatMessageId) {
      lastAutoScrollMessageIdRef.current = null
      return
    }
    if (lastAutoScrollMessageIdRef.current === lastChatMessageId) {
      return
    }
    lastAutoScrollMessageIdRef.current = lastChatMessageId
    const container = chatMessagesContainerRef.current
    if (container) {
      container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' })
    }
  }, [lastChatMessageId])

  useEffect(() => {
    if (!chatLoading) return
    const container = chatMessagesContainerRef.current
    if (container) {
      container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' })
    }
  }, [chatLoading, liveAgentStatus, liveAgentTimeline.length, liveAgentPreviewText.length])

  useEffect(() => {
    if (!chatLoading) return
    const timer = window.setInterval(() => {
      setLiveAgentElapsedSec((value) => value + 1)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [chatLoading])

  useEffect(() => {
    const el = liveOutputRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [liveAgentPreviewText])

  useEffect(() => {
    if (typeof window === 'undefined') return
    localStorage.setItem('doc_studio_right_panel_closed', String(rightPanelClosed))
  }, [rightPanelClosed])

  useEffect(() => {
    if (typeof window === 'undefined') return
    localStorage.setItem('doc_studio_left_panel_closed', String(leftPanelClosed))
  }, [leftPanelClosed])

  const rightPanelClosedRef = useRef(rightPanelClosed)
  rightPanelClosedRef.current = rightPanelClosed

  const hasActiveEditorSelection = useCallback(() => {
    const editor = editorRef.current
    if (!editor) return false
    const ranges = editor.getSelections?.() || []
    return ranges.some((range: any) => {
      if (!range) return false
      if (typeof range.isEmpty === 'function') return !range.isEmpty()
      return (
        range.startLineNumber !== range.endLineNumber ||
        range.startColumn !== range.endColumn
      )
    })
  }, [])

  const handleCtrlLShortcut = useCallback(() => {
    // Cursor-like behavior:
    // - If editor has a real selection, Ctrl+L links that range into the composer.
    // - If there is no selection, Ctrl+L opens/focuses the chat composer.
    // This avoids Monaco swallowing Ctrl+L while the right panel is closed.
    if (hasActiveEditorSelection()) {
      addSelectionSnippet()
      return
    }
    if (rightPanelClosedRef.current) {
      setRightPanelClosed(false)
      setRightTab('chat')
    } else {
      setRightTab('chat')
    }
    requestAnimationFrame(() => {
      promptInputDivRef.current?.focus()
    })
  }, [addSelectionSnippet, hasActiveEditorSelection])

  useEffect(() => {
    const handleGlobalKeyDown = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return
      const key = e.key.toLowerCase()
      if (key === 'l') {
        e.preventDefault()
        handleCtrlLShortcut()
      }
      if (key === 'b') {
        e.preventDefault()
        setLeftPanelClosed((prev) => !prev)
      }
    }
    document.addEventListener('keydown', handleGlobalKeyDown)
    return () => document.removeEventListener('keydown', handleGlobalKeyDown)
  }, [handleCtrlLShortcut])

  const handleLeftResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    setIsDraggingLeft(true)
  }, [])

  // ??????????
  const handleRightResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    setIsDraggingRight(true)
  }, [])

  // ??????
  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (isDraggingLeft) {
        const container = document.querySelector('.doc-studio')
        if (container) {
          const containerRect = container.getBoundingClientRect()
          const cursorWidth = e.clientX - containerRect.left
          const dynamicMaxLeft = Math.min(
            MAX_LEFT_SIDER_WIDTH,
            containerRect.width - rightSiderWidth - MIN_CENTER_WIDTH,
          )
          if (dynamicMaxLeft >= MIN_LEFT_SIDER_WIDTH) {
            const nextLeftWidth = Math.max(
              MIN_LEFT_SIDER_WIDTH,
              Math.min(dynamicMaxLeft, cursorWidth),
            )
            setLeftSiderWidth(nextLeftWidth)
          }
        }
      }
      if (isDraggingRight) {
        const container = document.querySelector('.doc-studio')
        if (container) {
          const containerRect = container.getBoundingClientRect()
          const cursorWidth = containerRect.right - e.clientX
          const dynamicMaxRight = Math.min(
            MAX_RIGHT_SIDER_WIDTH,
            containerRect.width - leftSiderWidth - MIN_CENTER_WIDTH,
          )
          if (dynamicMaxRight >= MIN_RIGHT_SIDER_WIDTH) {
            const nextRightWidth = Math.max(
              MIN_RIGHT_SIDER_WIDTH,
              Math.min(dynamicMaxRight, cursorWidth),
            )
            setRightSiderWidth(nextRightWidth)
          }
        }
      }
    }

    const handleMouseUp = () => {
      if (isDraggingLeft) {
        localStorage.setItem('latex_editor_left_sider_width', leftSiderWidth.toString())
        setIsDraggingLeft(false)
      }
      if (isDraggingRight) {
        localStorage.setItem('latex_editor_right_sider_width', rightSiderWidth.toString())
        setIsDraggingRight(false)
      }
    }

    if (isDraggingLeft || isDraggingRight) {
      document.addEventListener('mousemove', handleMouseMove)
      document.addEventListener('mouseup', handleMouseUp)
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
    }

    return () => {
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleMouseUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
  }, [isDraggingLeft, isDraggingRight, leftSiderWidth, rightSiderWidth])

  useEffect(() => {
    const clampPaneWidths = () => {
      const container = document.querySelector('.doc-studio')
      if (!container) return

      const containerRect = container.getBoundingClientRect()
      let nextLeftWidth = Math.max(
        MIN_LEFT_SIDER_WIDTH,
        Math.min(MAX_LEFT_SIDER_WIDTH, leftSiderWidth),
      )
      const maxRightByCurrentLeft = Math.min(
        MAX_RIGHT_SIDER_WIDTH,
        containerRect.width - nextLeftWidth - MIN_CENTER_WIDTH,
      )
      let nextRightWidth = Math.max(
        MIN_RIGHT_SIDER_WIDTH,
        Math.min(
          maxRightByCurrentLeft >= MIN_RIGHT_SIDER_WIDTH
            ? maxRightByCurrentLeft
            : MIN_RIGHT_SIDER_WIDTH,
          rightSiderWidth,
        ),
      )

      const maxLeftByCurrentRight = Math.min(
        MAX_LEFT_SIDER_WIDTH,
        containerRect.width - nextRightWidth - MIN_CENTER_WIDTH,
      )
      if (maxLeftByCurrentRight >= MIN_LEFT_SIDER_WIDTH) {
        nextLeftWidth = Math.min(nextLeftWidth, maxLeftByCurrentRight)
      }

      if (nextLeftWidth !== leftSiderWidth) {
        setLeftSiderWidth(nextLeftWidth)
      }
      if (nextRightWidth !== rightSiderWidth) {
        setRightSiderWidth(nextRightWidth)
      }
    }

    clampPaneWidths()
    window.addEventListener('resize', clampPaneWidths)
    return () => window.removeEventListener('resize', clampPaneWidths)
  }, [leftSiderWidth, rightSiderWidth])

  const handleWorkspaceChange = (workspaceId: string) => {
    navigate(`/doc-studio/${workspaceId}`)
  }

  const handleKnowledgeBaseChange = (value: number | string) => {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) {
      setSelectedKnowledgeBaseId(parsed)
      persistLastUsedKnowledgeBaseId(parsed)
    } else {
      setSelectedKnowledgeBaseId(null)
    }
  }

  const handleToggleRagEnabled = useCallback(() => {
    if (ragEnabled) {
      setRagEnabled(false)
      return
    }
    if (knowledgeLoading) {
      message.info('知识库加载中，请稍候')
      return
    }
    if (!knowledgeBases.length) {
      message.warning('暂无可用知识库，请先在知识库页面创建。')
      return
    }
    const targetKbId = resolvePreferredKnowledgeBaseId(knowledgeBases, [
      selectedKnowledgeBaseId,
      preferredKbFromUrl,
    ])
    if (targetKbId == null) {
      message.warning('暂无可用知识库，请先在知识库页面创建。')
      return
    }
    setSelectedKnowledgeBaseId(targetKbId)
    persistLastUsedKnowledgeBaseId(targetKbId)
    setRagEnabled(true)
  }, [
    ragEnabled,
    knowledgeLoading,
    knowledgeBases,
    selectedKnowledgeBaseId,
    preferredKbFromUrl,
  ])

  useEffect(() => {
    if (!ragEnabled) return
    if (!knowledgeBases.length) return
    if (
      selectedKnowledgeBaseId != null &&
      knowledgeBases.some((item) => item.id === selectedKnowledgeBaseId)
    ) {
      return
    }
    const targetKbId = resolvePreferredKnowledgeBaseId(knowledgeBases, [
      selectedKnowledgeBaseId,
      preferredKbFromUrl,
    ])
    if (targetKbId == null) return
    setSelectedKnowledgeBaseId(targetKbId)
    persistLastUsedKnowledgeBaseId(targetKbId)
  }, [ragEnabled, knowledgeBases, selectedKnowledgeBaseId, preferredKbFromUrl])

  const handleCreateWorkspace = async () => {
    if (!newWorkspaceFile) {
      message.warning('请选择剧本文件（docx / pdf / txt / md）')
      return
    }
    setWorkspaceSubmitting(true)
    try {
      // ScriptLens：单步上传即创建剧本工作区。name 留空时后端用文件名作 title。
      const workspace = await createWorkspace({
        name: newWorkspaceName.trim() || newWorkspaceFile.name.replace(/\.[^.]+$/, ''),
        config: { file: newWorkspaceFile },
      })
      setWorkspaceModalOpen(false)
      setNewWorkspaceName('')
      setNewWorkspaceType('latex')
      setNewWorkspaceFile(null)
      await loadWorkspaces(workspace.workspaceId)
      message.success('剧本已上传，正在解析与索引…片刻后场景列表会出现')
    } catch (error) {
      message.error(getErrorMessage(error))
    } finally {
      setWorkspaceSubmitting(false)
    }
  }

  // ????????????
  const findNode = useCallback((nodes: any, targetPath: string): any => {
    if (!nodes || !Array.isArray(nodes)) return null
    for (const node of nodes) {
      if (node.path === targetPath) return node
      if (node.children) {
        const found = findNode(node.children, targetPath)
        if (found) return found
      }
    }
    return null
  }, [])

  const handleTreeSelect = async (keys: React.Key[]) => {
    const path = String(keys[0] || '')
    if (!path) return
    setTreeFocusPath(path)
    
    const node = findNode(snap.fileTree, path)
    
    // ?????????????Tree ?????????/????
    if (node && node.type === 'directory') {
      return
    }
    
    // ???????????????????????
    if (snap.activeFilePath !== path) {
      await openFile(path)
    }
  }

  const openContextMenuAt = useCallback(
    (event: Pick<MouseEvent, 'clientX' | 'clientY'>, path: string, type: ContextMenuTargetType) => {
      setContextMenuPath(path)
      setContextMenuType(type)
      setContextMenuPosition({ x: event.clientX, y: event.clientY })
      setContextMenuVisible(true)
    },
    [],
  )

  const handleExplorerContextMenu = (event: React.MouseEvent) => {
    event.preventDefault()
    event.stopPropagation()
    openContextMenuAt(event, '', 'workspace')
  }

  // ??????
  const handleRightClick = (info: any) => {
    const { event, node } = info
    event.preventDefault()
    event.stopPropagation()

    const nodeData = findNode(snap.fileTree, node.key as string)
    const nodeType: ContextMenuTargetType = nodeData?.type === 'directory' ? 'directory' : 'file'
    setTreeFocusPath(node.key as string)
    openContextMenuAt(event, node.key as string, nodeType)
  }

  const openCreateModalAtPath = (type: 'file' | 'directory', baseDirectoryPath = '') => {
    if (baseDirectoryPath && isNotebookSystemPath(baseDirectoryPath, { protectParents: true })) {
      message.warning('Notebook 系统目录不允许手动创建或修改')
      setContextMenuVisible(false)
      return
    }
    const normalizedPath = baseDirectoryPath ? `${baseDirectoryPath.replace(/\/+$/, '')}/` : ''
    setFileModalType(type)
    setFileModalPath(normalizedPath)
    setFileModalContent('')
    setFileModalOpen(true)
    setContextMenuVisible(false)
  }

  const handleCreateFileInDirectory = (directoryPath: string) => {
    openCreateModalAtPath('file', directoryPath)
  }

  const handleCreateFolderInDirectory = (directoryPath: string) => {
    openCreateModalAtPath('directory', directoryPath)
  }
  
  // ??????????
  const handleUploadToDirectory = (directoryPath: string) => {
    if (isNotebookSystemPath(directoryPath, { protectParents: true })) {
      message.warning('Notebook 系统目录不允许手动上传文件')
      setContextMenuVisible(false)
      return
    }
    setContextMenuVisible(false)
    // ?????????????
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = '*/*'  // ?????????
    input.onchange = async (e: Event) => {
      const target = e.target as HTMLInputElement
      const file = target.files?.[0]
      if (!file || !snap.workspaceId) return
      
      setUploading(true)
      try {
        console.log('准备上传文件', {
          fileName: file.name,
          fileSize: file.size,
          directory: directoryPath,
          workspaceId: snap.workspaceId,
        })
        
        // ????????
        const result = await uploadFile({ 
          workspaceId: snap.workspaceId, 
          file,
          directory: directoryPath  // ?? directory ????????
        })
        
        console.log('上传完成响应:', result)
        message.success(
          `上传成功 ${directoryPath || '根目录'}: ${file.name} (${(file.size / 1024).toFixed(2)} KB)`,
        )
        
        // ???????????????????
        await new Promise(resolve => setTimeout(resolve, 500))
        
        // ??????
        console.log('刷新工作区文件...')
        await loadWorkspaceFiles(snap.workspaceId, false)
        console.log('刷新完成')
        
        // ????????????????????
        if (directoryPath && !expandedKeys.includes(directoryPath)) {
          setExpandedKeys(prev => [...prev, directoryPath])
        }
        
        // ???????????????
        if (
          file.name.endsWith('.tex') ||
          file.name.endsWith('.bib') ||
          file.name.endsWith('.md') ||
          file.name.endsWith('.markdown') ||
          file.name.endsWith('.txt')
        ) {
          const fullPath = directoryPath ? `${directoryPath}/${file.name}` : file.name
          setTimeout(() => openFile(fullPath), 500)  // ??????????????
        }
      } catch (error) {
        console.error('上传失败:', error)
        message.error(`上传失败: ${getErrorMessage(error)}`)
      } finally {
        setUploading(false)
      }
    }
    input.click()
  }
  
  // ??????
  useEffect(() => {
    const handleClick = () => setContextMenuVisible(false)
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setContextMenuVisible(false)
      }
    }
    if (contextMenuVisible) {
      document.addEventListener('click', handleClick)
      document.addEventListener('keydown', handleKeyDown)
      return () => {
        document.removeEventListener('click', handleClick)
        document.removeEventListener('keydown', handleKeyDown)
      }
    }
  }, [contextMenuVisible])

  const handleTabChange = async (key: string) => {
    if (!key) return
    await openFile(key)
  }

  const handleTabEdit = (targetKey: string | React.MouseEvent | React.KeyboardEvent, action: 'add' | 'remove') => {
    if (action === 'remove' && typeof targetKey === 'string') {
      docStudioActions.closeFile(targetKey)
      if (docStudioState.workspaceId && docStudioState.activeFilePath && !docStudioState.files[docStudioState.activeFilePath]) {
        openFile(docStudioState.activeFilePath, true)
      }
    }
  }

  const handleEditorChange = useCallback((value?: string) => {
    if (!snap.activeFilePath) return
    // ????????????????????????
    const currentContent = snap.files[snap.activeFilePath]?.content ?? ''
    if (value !== currentContent) {
    docStudioActions.updateFileContent(snap.activeFilePath, value ?? '')
    }
  }, [snap.activeFilePath, snap.files])

  const handleEditorMount = useCallback(
    (editorInstance: any) => {
      editorRef.current = editorInstance

      // ??????
      editorInstance.focus()

      // ???????Ctrl+A / Ctrl+L
      if (typeof window !== 'undefined' && (window as any).monaco) {
        const monaco = (window as any).monaco

        // Ctrl+A ???????
        editorInstance.addCommand(
          monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyA,
          () => {
            const model = editorInstance.getModel()
            if (model) {
              editorInstance.setSelection(model.getFullModelRange())
            }
          },
        )

        // Ctrl+L ????????????
        editorInstance.addCommand(
          monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyL,
          () => {
            handleCtrlLShortcut()
          },
        )
      }
    },
    [handleCtrlLShortcut],
  )

  const handleSave = useCallback(
    async (options?: { silent?: boolean }) => {
      if (!snap.workspaceId || !snap.activeFilePath) {
        if (!options?.silent) {
          message.warning('请先选择工作区文件')
        }
        return false
      }
      const buffer = docStudioState.files[snap.activeFilePath]
      if (!buffer || buffer.loading || !buffer.dirty) {
        return true
      }
      if (saveInFlightRef.current) {
        return false
      }

      const path = snap.activeFilePath
      const contentToSave = buffer.content || ''
      const encodingToSave = buffer.encoding

      saveInFlightRef.current = true
      try {
        await updateFileContent({
          workspaceId: snap.workspaceId,
          path,
          content: contentToSave,
          encoding: encodingToSave,
        }, {
          loading: false,
          errorToast: false,
        })
        docStudioActions.markFileSaved(path, contentToSave)
        return true
      } catch (error) {
        if (!options?.silent) {
          message.error(getErrorMessage(error))
        }
        return false
      } finally {
        saveInFlightRef.current = false
      }
    },
    [snap.activeFilePath, snap.workspaceId],
  )

  useEffect(() => {
    const handleSaveShortcut = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey)) return
      if (event.key.toLowerCase() !== 's') return
      event.preventDefault()
      void handleSave({ silent: true })
    }
    document.addEventListener('keydown', handleSaveShortcut)
    return () => document.removeEventListener('keydown', handleSaveShortcut)
  }, [handleSave])

  useEffect(() => {
    if (!snap.workspaceId || !snap.activeFilePath) return
    if (!currentFileBuffer || currentFileBuffer.loading || !currentFileBuffer.dirty) return
    const timer = window.setTimeout(() => {
      void handleSave({ silent: true })
    }, 900)
    return () => window.clearTimeout(timer)
  }, [
    currentFileBuffer?.content,
    currentFileBuffer?.dirty,
    currentFileBuffer?.encoding,
    currentFileBuffer?.loading,
    handleSave,
    snap.activeFilePath,
    snap.workspaceId,
  ])

  const handleCompile = async () => {
    if (!snap.workspaceId) {
      message.warning('请先选择工作区')
      return
    }
    if (!supportsCompilePanel) {
      message.info('当前文件类型不支持编译')
      return
    }
    if (isPlaintextActiveFile) {
      message.info('TXT 文件无需编译')
      return
    }
    if (isMarkdownActiveFile) {
      const markdownPath = String(snap.activeFilePath || '').trim()
      if (!markdownPath) {
        message.warning('请先打开 Markdown 文件')
        return
      }
      const markdownContent = String(docStudioState.files[markdownPath]?.content || '')
      const markdownResult = buildMarkdownCompileResult(markdownPath, markdownContent)
      docStudioActions.setCompileResult(markdownResult)
      setRightTab('compile')
      if (markdownResult.success) {
        message.success(markdownResult.summary || 'Markdown 检查通过')
      } else {
        message.error(markdownResult.error || markdownResult.summary || 'Markdown 检查失败')
      }
      return
    }

    // ?Cursor ???????????? .tex ?????????????? main_file
    const activeTexFile =
      snap.activeFilePath && snap.activeFilePath.toLowerCase().endsWith('.tex')
        ? snap.activeFilePath
        : undefined
    const configuredMainFile =
      (snap.workspaceConfig?.main_file as string | undefined) ||
      (snap.workspaceConfig?.mainFile as string | undefined)

    const mainFile = activeTexFile || configuredMainFile

    if (!mainFile) {
      message.warning('未找到 .tex 主文件')
      return
    }

    // ??????????????
    console.log('编译入口文件:', {
      activeFilePath: snap.activeFilePath,
      activeTexFile,
      configuredMainFile,
      finalMainFile: mainFile,
    })

    try {
      const result = await compileWorkspace({
        workspaceId: snap.workspaceId,
        mainFile,
      })
      docStudioActions.setCompileResult(result)
      setRightTab('compile')
      if (result.success) {
        message.success(result.summary || '编译成功')
      } else {
        // ???????????"????????
        const allErrors = result.data?.errors || []
        const missingFiles = allErrors
          .filter((err: string) => err.includes('not found') || err.includes('文件不存在'))
          .map((err: string) => {
            const match = err.match(/File `([^']+)'|文件不存在\s*(.+)/)
            return match ? (match[1] || match[2]) : null
          })
          .filter(Boolean) as string[]
        
        if (missingFiles.length > 0) {
          message.error({
            content: (
              <div>
                <div style={{ marginBottom: 8, fontWeight: 'bold' }}>缺失文件:</div>
                <ul style={{ margin: 0, paddingLeft: 20 }}>
                  {missingFiles.map((file, idx) => (
                    <li key={idx} style={{ marginBottom: 4 }}>
                      <code>{file}</code>
                    </li>
                  ))}
                </ul>
                <div style={{ marginTop: 8, fontSize: 12, color: '#666' }}>
                  请确认引用的 .tex 文件是否存在
                </div>
              </div>
            ),
            duration: 10,
          })
        } else {
          const firstError = result.error || allErrors[0]
          message.error(firstError ? `编译失败: ${firstError}` : '编译失败')
        }
      }
    } catch (error) {
      message.error(getErrorMessage(error))
    }
  }

  useEffect(() => {
    if (!autoCompileFromUrl || autoCompileHandledRef.current) return
    if (!snap.workspaceId || !snap.activeFilePath) return
    if (preferredFileFromUrl && snap.activeFilePath !== preferredFileFromUrl) return
    if (!currentFileBuffer || currentFileBuffer.loading) return

    const activePath = String(snap.activeFilePath || '').trim()
    const extension = getFileExtension(activePath)
    if (extension !== '.md' && extension !== '.markdown') {
      autoCompileHandledRef.current = true
      clearAutoCompileFlagFromUrl()
      return
    }

    const markdownResult = buildMarkdownCompileResult(activePath, currentFileBuffer.content || '')
    docStudioActions.setCompileResult(markdownResult)
    setRightPanelClosed(false)
    setRightTab('compile')
    autoCompileHandledRef.current = true
    clearAutoCompileFlagFromUrl()
    if (markdownResult.success) {
      message.success(markdownResult.summary || 'Markdown 检查通过')
    } else {
      message.error(markdownResult.error || markdownResult.summary || 'Markdown 检查失败')
    }
  }, [
    autoCompileFromUrl,
    clearAutoCompileFlagFromUrl,
    currentFileBuffer,
    preferredFileFromUrl,
    snap.activeFilePath,
    snap.workspaceId,
  ])

  const handlePreviewPdf = async () => {
    if (!snap.workspaceId) return
    try {
      const blob = await downloadPdf({ workspaceId: snap.workspaceId })
      const url = URL.createObjectURL(blob)
    window.open(url, '_blank')
      // ???? URL??????????
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error) {
      message.error(getErrorMessage(error))
    }
  }

  const handleDownloadPdf = async () => {
    if (!snap.workspaceId) return
    try {
      const blob = await downloadPdf({ workspaceId: snap.workspaceId })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'output.pdf'
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      message.success('PDF 下载成功')
    } catch (error) {
      message.error(getErrorMessage(error))
    }
  }

  const handleDownloadFileAtPath = async (filePath: string) => {
    if (!snap.workspaceId || !filePath) return
    try {
      const blob = await downloadFile({
        workspaceId: snap.workspaceId,
        filePath,
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filePath.split('/').pop() || 'file'
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      message.success('文件已下载')
    } catch (error) {
      message.error(getErrorMessage(error))
    }
  }

  const handleDownloadCurrentFile = async () => {
    if (!snap.activeFilePath) return
    await handleDownloadFileAtPath(snap.activeFilePath)
  }
  
  // ????????????????
  const handleDeleteFromTree = async (path: string, type: 'file' | 'directory') => {
    if (!snap.workspaceId) return
    if (type === 'directory' && isNotebookSystemPath(path, { protectParents: true })) {
      message.warning('Notebook 系统目录不允许删除')
      setContextMenuVisible(false)
      return
    }
    try {
      await deleteFile({
        workspaceId: snap.workspaceId,
        path: path,
      })
      message.success(`已删除${type === 'file' ? '文件' : '文件夹'}`)
      
      // ??????????????????
      if (type === 'file' && snap.activeFilePath === path) {
        docStudioActions.setActiveFile('')
      }
      
      // ??????
      await loadWorkspaceFiles(snap.workspaceId, false)
      setContextMenuVisible(false)
    } catch (error) {
      message.error(getErrorMessage(error))
      setContextMenuVisible(false)
    }
  }

  const showDeleteConfirm = useCallback(
    (path: string, type: 'file' | 'directory') => {
      setContextMenuVisible(false)
      if (type === 'directory' && isNotebookSystemPath(path, { protectParents: true })) {
        message.warning('Notebook 系统目录不允许删除')
        return
      }
      const label = type === 'directory' ? '文件夹' : '文件'
      const detail =
        type === 'directory'
          ? `确定删除文件夹 "${path}" 及其所有内容？`
          : `确定删除文件 "${path}"？`
      Modal.confirm({
        title: `确认删除${label}？`,
        content: detail,
        okText: '删除',
        okType: 'danger',
        cancelText: '取消',
        onOk: () => handleDeleteFromTree(path, type),
      })
    },
    [handleDeleteFromTree, isNotebookSystemPath],
  )

  const openRenameModal = useCallback((path: string, type: 'file' | 'directory') => {
    if (isNotebookSystemPath(path, { protectParents: type === 'directory' })) {
      message.warning('Notebook 系统目录不允许重命名或移动')
      setContextMenuVisible(false)
      return
    }
    const { normalized, name } = splitWorkspacePath(path)
    if (!normalized || !name) {
      message.warning('无法重命名该路径')
      return
    }
    setRenameSourcePath(normalized)
    setRenameSourceType(type)
    setRenameNameInput(name)
    setTreeFocusPath(normalized)
    setRenameModalOpen(true)
    setContextMenuVisible(false)
  }, [isNotebookSystemPath])

  useEffect(() => {
    const handleF2Rename = (event: KeyboardEvent) => {
      if (event.key !== 'F2') return
      const target = event.target as HTMLElement | null
      if (
        target &&
        (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
      ) {
        return
      }
      if (renameModalOpen || fileModalOpen || workspaceModalOpen || diffModalOpen || agentDiffReviewOpen) return

      const candidatePath = treeFocusPath || snap.activeFilePath
      if (!candidatePath) return
      const node = findNode(snap.fileTree, candidatePath)
      if (!node || (node.type !== 'file' && node.type !== 'directory')) return

      event.preventDefault()
      openRenameModal(candidatePath, node.type)
    }

    document.addEventListener('keydown', handleF2Rename)
    return () => document.removeEventListener('keydown', handleF2Rename)
  }, [
    agentDiffReviewOpen,
    diffModalOpen,
    fileModalOpen,
    findNode,
    openRenameModal,
    renameModalOpen,
    snap.activeFilePath,
    snap.fileTree,
    treeFocusPath,
    workspaceModalOpen,
  ])

  const applyRenamedPathsToFrontendState = (
    sourcePath: string,
    targetPath: string,
    sourceType: 'file' | 'directory',
  ) => {
    const remap = (path: string) => remapPathWithPrefix(path, sourcePath, targetPath, sourceType)

    const nextOpenedFiles = snap.openedFiles.map(remap)
    const dedupOpenedFiles: string[] = []
    nextOpenedFiles.forEach((path) => {
      if (path && !dedupOpenedFiles.includes(path)) {
        dedupOpenedFiles.push(path)
      }
    })
    if (
      dedupOpenedFiles.length !== snap.openedFiles.length ||
      dedupOpenedFiles.some((path, index) => path !== snap.openedFiles[index])
    ) {
      docStudioActions.setOpenedFiles(dedupOpenedFiles)
    }

    const nextActivePath = remap(snap.activeFilePath || '')
    if (nextActivePath !== (snap.activeFilePath || '')) {
      docStudioActions.setActiveFile(nextActivePath)
    }

    const nextFileBuffers: Record<string, any> = {}
    let changed = false
    Object.entries(docStudioState.files).forEach(([path, buffer]) => {
      const mappedPath = remap(path)
      nextFileBuffers[mappedPath] = buffer
      if (mappedPath !== path) {
        changed = true
      }
    })
    if (changed) {
      docStudioState.files = nextFileBuffers as any
    }
  }

  const handleRenamePath = async () => {
    if (!snap.workspaceId || !renameSourcePath) return
    if (isNotebookSystemPath(renameSourcePath, { protectParents: renameSourceType === 'directory' })) {
      message.warning('Notebook 系统目录不允许重命名或移动')
      return
    }
    const nextName = renameNameInput.trim()
    if (!nextName) {
      message.warning('请输入新的名称')
      return
    }
    if (nextName === '.' || nextName === '..') {
      message.warning('名称不合法')
      return
    }
    if (/[\\/]/.test(nextName)) {
      message.warning('名称不能包含斜杠')
      return
    }

    const { name: currentName, parentPath } = splitWorkspacePath(renameSourcePath)
    if (!currentName) {
      message.warning('源路径无效')
      return
    }
    if (nextName === currentName) {
      setRenameModalOpen(false)
      return
    }

    const targetPath = parentPath ? `${parentPath}/${nextName}` : nextName
    if (isNotebookSystemPath(targetPath)) {
      message.warning('目标路径位于 Notebook 系统目录，不允许移动')
      return
    }
    setRenameSubmitting(true)
    try {
      await renameFileOrDirectory({
        workspaceId: snap.workspaceId,
        sourcePath: renameSourcePath,
        targetPath,
      })
      applyRenamedPathsToFrontendState(renameSourcePath, targetPath, renameSourceType)
      setTreeFocusPath(targetPath)
      setRenameModalOpen(false)
      setRenameSourcePath('')
      setRenameNameInput('')
      await refreshFileTree(false)
      message.success('重命名成功')
    } catch (error) {
      message.error(getErrorMessage(error))
    } finally {
      setRenameSubmitting(false)
    }
  }

  const openFileModal = (type: 'file' | 'directory') => {
    setFileModalType(type)
    setFileModalPath('')
    setFileModalContent('')
    setFileModalOpen(true)
  }

  const handleCreateFile = async () => {
    const targetPath = fileModalPath.trim()
    if (!snap.workspaceId || !targetPath) {
      message.warning('请输入文件路径')
      return
    }
    if (isNotebookSystemPath(targetPath)) {
      message.warning('该路径为 Notebook 系统目录，不允许手动创建')
      return
    }
    if (fileModalType === 'file' && /[\\/]$/.test(targetPath)) {
      message.warning('新建文件时路径必须包含文件名，例如 sections/test.md')
      return
    }
    setFileSubmitting(true)
    try {
      await createFileOrDirectory({
        workspaceId: snap.workspaceId,
        path: targetPath,
        type: fileModalType,
        content: fileModalType === 'file' ? fileModalContent : undefined,
      })
      setFileModalOpen(false)
      message.success('创建成功')
      await loadWorkspaceFiles(snap.workspaceId, false)
      
      // ???????
      const pathParts = targetPath.split('/')
      if (pathParts.length > 1) {
        const parentPath = pathParts.slice(0, -1).join('/')
        if (parentPath && !expandedKeys.includes(parentPath)) {
          setExpandedKeys(prev => [...prev, parentPath])
        }
      }
      
      if (fileModalType === 'file') {
        setTimeout(() => openFile(targetPath), 300)
      }
    } catch (error) {
      message.error(getErrorMessage(error))
    } finally {
      setFileSubmitting(false)
    }
  }

  const refreshFileTree = useCallback(
    async (showToast = true) => {
      if (!snap.workspaceId) return
      await loadWorkspaceFiles(snap.workspaceId, false)
      if (showToast) {
        message.success('文件列表已刷新')
      }
    },
    [loadWorkspaceFiles, snap.workspaceId],
  )

  const handleWorkspaceUploadClick = () => {
    fileInputRef.current?.click()
  }

  const handleFileInputChange = async (event: React.ChangeEvent<HTMLInputElement>) => {
    if (!snap.workspaceId) return
    const file = event.target.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      await uploadFile({ workspaceId: snap.workspaceId, file })
      message.success('上传成功')
      await loadWorkspaceFiles(snap.workspaceId, false)
    } catch (error) {
      message.error(getErrorMessage(error))
    } finally {
      setUploading(false)
      event.target.value = ''
    }
  }

  const buildChatImageAttachmentsFromFiles = useCallback(async (files: File[]) => {
        const incoming: ChatImageAttachment[] = []
    for (const file of files) {
          if (!file.type.startsWith('image/')) {
            message.warning(`已跳过非图片文件：${file.name || 'unknown'}`)
            continue
          }
          if (file.size > MAX_CHAT_IMAGE_FILE_SIZE) {
            message.warning(
              `图片过大（>${Math.round(MAX_CHAT_IMAGE_FILE_SIZE / 1024 / 1024)}MB）：${file.name || 'unnamed'}`,
            )
            continue
          }
          // eslint-disable-next-line no-await-in-loop
          const dataUrl = await readFileAsDataUrl(file)
          incoming.push({
            id: generateId(),
            name: file.name || `image-${Date.now()}.png`,
            mimeType: file.type || 'image/png',
            size: file.size,
            dataUrl,
          })
        }
    return incoming
  }, [])

  const appendChatImageFiles = useCallback(
    async (files: File[]) => {
      if (!files.length) return
      const remain = MAX_CHAT_IMAGE_COUNT - chatImageAttachments.length
      if (remain <= 0) {
        message.warning(`最多可添加 ${MAX_CHAT_IMAGE_COUNT} 张图片`)
        return
      }

      const candidates = files.slice(0, remain)
      if (files.length > remain) {
        message.info(`最多可添加 ${MAX_CHAT_IMAGE_COUNT} 张图片，已截取前 ${remain} 张`)
      }

      setChatImageProcessing(true)
      try {
        const incoming = await buildChatImageAttachmentsFromFiles(candidates)
        if (!incoming.length) return
        setChatImageAttachments((prev) => {
          const used = new Set(prev.map((item) => `${item.name}::${item.size}`))
          const deduped = incoming.filter((item) => {
            const key = `${item.name}::${item.size}`
            if (used.has(key)) return false
            used.add(key)
            return true
          })
          return deduped.length ? [...prev, ...deduped] : prev
        })
      } finally {
        setChatImageProcessing(false)
      }
    },
    [buildChatImageAttachmentsFromFiles, chatImageAttachments.length],
  )

  const handleChatImagePickerClick = useCallback(() => {
    chatImageInputRef.current?.click()
  }, [])

  const handleChatImageInputChange = useCallback(
    async (event: React.ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files || [])
      if (files.length) {
        await appendChatImageFiles(files)
      }
      event.target.value = ''
    },
    [appendChatImageFiles],
  )

  const handlePromptPaste = useCallback(
    async (event: React.ClipboardEvent<HTMLDivElement>) => {
      const items = Array.from(event.clipboardData?.items || [])
      const imageFiles: File[] = []
      items.forEach((item) => {
        if (item.type.startsWith('image/')) {
          const file = item.getAsFile()
          if (file) imageFiles.push(file)
        }
      })
      if (!imageFiles.length) return
      event.preventDefault()
      await appendChatImageFiles(imageFiles)
    },
    [appendChatImageFiles],
  )

  const removeChatImageAttachment = useCallback((id: string) => {
    setChatImageAttachments((prev) => prev.filter((item) => item.id !== id))
  }, [])

  const appendReEditImageFiles = useCallback(
    async (files: File[]) => {
      if (!files.length || !reEditDraft) return
      const remain = MAX_CHAT_IMAGE_COUNT - reEditDraft.images.length
      if (remain <= 0) {
        message.warning(`最多可添加 ${MAX_CHAT_IMAGE_COUNT} 张图片`)
        return
      }
      const candidates = files.slice(0, remain)
      if (files.length > remain) {
        message.info(`最多可添加 ${MAX_CHAT_IMAGE_COUNT} 张图片，已截取前 ${remain} 张`)
      }
      const expectedMessageId = reEditDraft.messageId
      const incoming = await buildChatImageAttachmentsFromFiles(candidates)
      if (!incoming.length) return
      setReEditDraft((prev) => {
        if (!prev || prev.messageId !== expectedMessageId) return prev
        const used = new Set(prev.images.map((item) => `${item.name}::${item.size}`))
        const deduped = incoming.filter((item) => {
          const key = `${item.name}::${item.size}`
          if (used.has(key)) return false
          used.add(key)
          return true
        })
        if (!deduped.length) return prev
        return { ...prev, images: [...prev.images, ...deduped] }
      })
    },
    [buildChatImageAttachmentsFromFiles, reEditDraft],
  )

  const removeReEditImageAttachment = useCallback((id: string) => {
    setReEditDraft((prev) => {
      if (!prev) return prev
      return { ...prev, images: prev.images.filter((item) => item.id !== id) }
    })
  }, [])

  const handleReEditPromptPaste = useCallback(
    async (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const items = Array.from(event.clipboardData?.items || [])
      const imageFiles: File[] = []
      items.forEach((item) => {
        if (item.type.startsWith('image/')) {
          const file = item.getAsFile()
          if (file) imageFiles.push(file)
        }
      })
      if (!imageFiles.length) return
      event.preventDefault()
      await appendReEditImageFiles(imageFiles)
    },
    [appendReEditImageFiles],
  )

  const handleUndoLastApply = async () => {
    if (!snap.workspaceId) {
      message.warning('请先选择工作区')
      return
    }
    if (!lastOperationId) {
      message.info('没有可撤销的操作')
      return
    }
    setUndoingLastApply(true)
    try {
      const result = await revertOperation(
        {
          workspaceId: snap.workspaceId,
          operationId: lastOperationId,
        },
        {
          loading: false,
          errorToast: false,
        },
      )
      const revertedFiles = result.reverted_files || []
      for (const filePath of revertedFiles) {
        if (snap.openedFiles.includes(filePath)) {
          await openFile(filePath, true, true)
        }
      }
      if (result.deleted_files?.length) {
        await loadWorkspaceFiles(snap.workspaceId, false)
      }
      const affectedCount = (result.reverted_files?.length || 0) + (result.deleted_files?.length || 0)
      if (affectedCount) {
        message.success(`已回滚 ${affectedCount} 个变更`)
      } else {
        message.info('没有可回滚的变更')
      }
      setLastOperationId(null)
    } catch (error) {
      message.error(`回滚失败: ${getErrorMessage(error)}`)
    } finally {
      setUndoingLastApply(false)
    }
  }

  const loadOperationHistory = useCallback(async () => {
    if (!snap.workspaceId) return
    setOperationHistoryLoading(true)
    try {
      const data = await listOperations(
        { workspaceId: snap.workspaceId },
        {
          loading: false,
          errorToast: false,
        },
      )
      setOperationHistory(Array.isArray(data) ? data : [])
    } catch (error) {
      message.error('加载历史失败')
    } finally {
      setOperationHistoryLoading(false)
    }
  }, [snap.workspaceId])

  const refreshSystemStats = useCallback(
    async (silent?: boolean) => {
      if (!snap.workspaceId) return
      if (!silent) {
        setSystemStatsLoading(true)
      }
      try {
        const [metrics, health] = await Promise.all([
          fetchMetricsSummary({ loading: false, errorToast: false }),
          fetchLlmHealth({ loading: false, errorToast: false }),
        ])
        setMetricsSummary(metrics)
        setLlmHealth(health)
      } catch (error) {
        if (!silent) {
          message.error('系统状态获取失败')
        }
      } finally {
        setSystemStatsLoading(false)
      }
    },
    [snap.workspaceId],
  )

  useEffect(() => {
    if (rightTab !== 'history') return
    loadOperationHistory()
  }, [loadOperationHistory, rightTab])

  useEffect(() => {
    if (!snap.workspaceId) return
    refreshSystemStats(true)
  }, [snap.workspaceId, refreshSystemStats])

  useEffect(() => {
    if (!systemStatusOpen) return
    refreshSystemStats()
  }, [systemStatusOpen, refreshSystemStats])

  const refreshLineChanges = useCallback(() => {
    if (!diffEditorRef.current) return
    const changes = diffEditorRef.current.getLineChanges?.() || []
    setLineChanges(changes)
    setCurrentHunkIndex((index) => {
      if (!changes.length) return 0
      return Math.min(index, changes.length - 1)
    })
  }, [])

  const resolveHunkLineRange = useCallback((change: any, lineCount: number) => {
    const modifiedStart = Number(change?.modifiedStartLineNumber || 0)
    const modifiedEnd = Number(change?.modifiedEndLineNumber || 0)
    const originalStart = Number(change?.originalStartLineNumber || 0)
    const baseStart = modifiedStart || originalStart || 1
    const startLine = Math.max(1, Math.min(baseStart, lineCount || 1))
    const baseEnd = modifiedEnd || startLine
    const endLine = Math.max(startLine, Math.min(baseEnd, lineCount || 1))
    return { startLine, endLine }
  }, [])

  const focusCurrentHunk = useCallback(() => {
    const diffEditor = diffEditorRef.current
    if (!diffEditor) return
    const modifiedEditor =
      typeof diffEditor.getModifiedEditor === 'function'
        ? diffEditor.getModifiedEditor()
        : diffEditor
    if (!modifiedEditor?.getModel) return
    const model = modifiedEditor.getModel()
    if (!model) return

    const clearDecorations = () => {
      if (typeof modifiedEditor.deltaDecorations !== 'function') return
      diffHunkDecorationsRef.current = modifiedEditor.deltaDecorations(diffHunkDecorationsRef.current, [])
    }

    const change = lineChanges[currentHunkIndex]
    if (!change) {
      clearDecorations()
      return
    }

    const lineCount = model.getLineCount?.() || 1
    const { startLine, endLine } = resolveHunkLineRange(change, lineCount)

    if (typeof modifiedEditor.revealLineInCenter === 'function') {
      modifiedEditor.revealLineInCenter(startLine)
    }
    if (typeof modifiedEditor.deltaDecorations === 'function') {
      diffHunkDecorationsRef.current = modifiedEditor.deltaDecorations(diffHunkDecorationsRef.current, [
        {
          range: {
            startLineNumber: startLine,
            startColumn: 1,
            endLineNumber: endLine,
            endColumn: model.getLineMaxColumn(endLine),
          },
          options: {
            isWholeLine: true,
            className: 'doc-studio__review-hunk-active-line',
            linesDecorationsClassName: 'doc-studio__review-hunk-active-gutter',
          },
        },
      ])
    }
  }, [currentHunkIndex, lineChanges, resolveHunkLineRange])

  const loadDiffContent = useCallback(
    async (diff: DocStudioAPI.FileDiff | undefined) => {
      if (!diff) {
        setResolvedOriginal('')
        setResolvedModified('')
        setLineChanges([])
        return
      }
      if (!snap.workspaceId) return
      const fallbackOriginal = diff.original_content || ''
      const fallbackModified = diff.modified_content || ''

      if (!diffOperationId || !diff.file_path) {
        setResolvedOriginal(fallbackOriginal)
        setResolvedModified(fallbackModified)
        return
      }

      setResolvedOriginal(fallbackOriginal)
      setResolvedModified(fallbackModified)
      try {
        const [snapshot, current] = await Promise.all([
          fetchOperationSnapshotFile(
            {
            workspaceId: snap.workspaceId,
            operationId: diffOperationId,
            filePath: diff.file_path,
            version: 'before',
            },
            { loading: false, errorToast: false },
          ),
          fetchFileContent(
            { workspaceId: snap.workspaceId, path: diff.file_path },
            { loading: false, errorToast: false },
          ),
        ])
        const originalContent = snapshot?.content ?? fallbackOriginal
        const modifiedContent = current?.content ?? fallbackModified
        setResolvedOriginal(originalContent)
        setResolvedModified(modifiedContent)
      } catch {
        // 保持 fallback，不闪烁
      }
    },
    [diffOperationId, snap.workspaceId],
  )

  useEffect(() => {
    loadDiffContent(allFileDiffs[currentDiffIndex])
  }, [allFileDiffs, currentDiffIndex, loadDiffContent])

  useEffect(() => {
    setCurrentHunkIndex(0)
  }, [currentDiffIndex])

  useEffect(() => {
    if (!resolvedOriginal && !resolvedModified) return
    requestAnimationFrame(() => {
      refreshLineChanges()
    })
  }, [resolvedOriginal, resolvedModified, refreshLineChanges])

  useEffect(() => {
    requestAnimationFrame(() => {
      focusCurrentHunk()
    })
  }, [currentHunkIndex, lineChanges, resolvedModified, focusCurrentHunk])

  const confirmOverwriteDirtyFiles = useCallback(async () => {
    const dirtyFilePaths = Object.entries(docStudioState.files || {})
      .filter(([, buffer]) => Boolean((buffer as any)?.dirty))
      .map(([filePath]) => filePath)
    if (!dirtyFilePaths.length) return
    await new Promise<void>((resolve, reject) => {
      Modal.confirm({
        title: '检测到未保存修改',
        content: (
          <div style={{ paddingTop: 4 }}>
            <p style={{ marginBottom: 8 }}>
              当前有 {dirtyFilePaths.length} 个文件存在未保存内容，恢复快照后这些本地改动可能被覆盖。
            </p>
            <p style={{ color: '#6b7280', fontSize: 13, marginBottom: 0 }}>
              建议先保存文件，或选择“继续但不恢复”。
            </p>
          </div>
        ),
        okText: '仍然恢复',
        cancelText: '取消',
        okType: 'primary',
        onOk: () => resolve(),
        onCancel: () => reject(new Error('__user_abort__')),
      })
    })
  }, [])

  const confirmReEditRestoreAction = useCallback(async (hasCheckpoint: boolean) => {
    await new Promise<void>((resolve, reject) => {
      Modal.confirm({
        title: '确认继续并恢复？',
        okType: 'danger',
        okText: '确认继续并恢复',
        cancelText: '取消',
        content: (
          <div style={{ paddingTop: 4 }}>
            <p style={{ marginBottom: 8 }}>
              该操作会清理当前消息之后的全部对话，并尝试将工作区文件回退到该消息发送前的状态。
            </p>
            <p style={{ color: '#b42318', fontSize: 13, marginBottom: 0 }}>
              这是敏感操作，后续修改可能丢失。建议先保存或备份重要修改。
            </p>
            {!hasCheckpoint && (
              <p style={{ color: '#6b7280', fontSize: 13, marginTop: 8, marginBottom: 0 }}>
                当前消息无 checkpoint，确认后将自动降级为仅清理后续对话。
              </p>
            )}
          </div>
        ),
        onOk: () => resolve(),
        onCancel: () => reject(new Error('__user_abort__')),
      })
    })
  }, [])

  const handleReEditMessage = useCallback(
    (msg: DocStudioChatMessage, msgIndex: number) => {
      if (!snap.workspaceId) {
        message.warning('请先选择工作区')
        return
      }
      if (chatLoading || reEditSubmitting) {
        message.warning('任务执行中，请稍后再试')
        return
      }
      const rawText = (msg.content || '')
        .replace(/\n*\[已附带图片\s*\d+\s*张\]\s*/g, '')
        .trim()
      const runId = typeof msg.meta?.runId === 'string' ? msg.meta.runId : ''
      const beforeMessageId =
        typeof msg.meta?.messageId === 'string' ? msg.meta.messageId.trim() : ''
      const prevImagesRaw = Array.isArray(msg.meta?.images) ? msg.meta.images : []
      const prevImages: ChatImageAttachment[] = prevImagesRaw
        .map((img: any, idx: number) => ({
          id: String(img?.id || `${Date.now()}-${idx}`),
          name: String(img?.name || `image-${idx + 1}`),
          dataUrl: String(img?.dataUrl || img?.data_url || ''),
          mimeType: String(img?.mimeType || 'image/png'),
          size: Number(img?.size || 0),
        }))
        .filter((item) => Boolean(item.dataUrl))
      const prevSelections = normalizeSelectionFragments(msg.meta?.selections)
      const prevFileMentions = normalizeFileMentionFragments(
        msg.meta?.fileMentions ?? msg.meta?.file_mentions,
      )
      setReEditDraft({
        messageId: msg.id,
        msgIndex: Math.max(0, Math.floor(msgIndex)),
        prompt: rawText,
        runId,
        beforeMessageId,
        images: prevImages,
        selections: prevSelections,
        fileMentions: prevFileMentions,
      })
    },
    [chatLoading, reEditSubmitting, snap.workspaceId],
  )

  const handleCancelReEdit = useCallback(() => {
    if (reEditSubmitting) return
    setReEditDraft(null)
  }, [reEditSubmitting])

  useEffect(() => {
    if (!reEditDraft) return
    const handleOutsideMouseDown = (event: MouseEvent) => {
      if (reEditSubmitting) return
      const target = event.target as Node | null
      const container = reEditContainerRef.current
      if (!container || !target) return
      if (container.contains(target)) return
      setReEditDraft(null)
    }
    document.addEventListener('mousedown', handleOutsideMouseDown, true)
    return () => {
      document.removeEventListener('mousedown', handleOutsideMouseDown, true)
    }
  }, [reEditDraft, reEditSubmitting])

  const handleSubmitReEdit = useCallback(
    async (restoreFiles: boolean) => {
      if (!snap.workspaceId) {
        message.warning('请先选择工作区')
        return
      }
      if (chatLoading || reEditSubmitting) {
        message.warning('任务执行中，请稍后再试')
        return
      }
      if (!reEditDraft) {
        message.warning('请先编辑一条历史消息')
        return
      }
      const submitPrompt = reEditDraft.prompt.trim()
      if (!submitPrompt && reEditDraft.images.length === 0) {
        message.warning('请输入指令或添加图片')
        return
      }

      const safeMsgIndex = Math.max(0, Math.floor(reEditDraft.msgIndex))
      const keepUserTurns = snap.chatMessages
        .slice(0, safeMsgIndex)
        .filter((item) => item.role === 'user').length
      const currentDraft = reEditDraft

      setReEditSubmitting(true)
      try {
        if (restoreFiles) {
          await confirmReEditRestoreAction(Boolean(currentDraft.runId))
          await confirmOverwriteDirtyFiles()
        }

        const sessionId = String(
          docStudioState.workspaceConfig?.session_id ||
            docStudioState.workspaceConfig?.sessionId ||
            '',
        ).trim()
        if (!sessionId) {
          docStudioActions.truncateMessagesFromIndex(safeMsgIndex)
        } else {
          await rewindConversation(
            {
              workspaceId: snap.workspaceId,
              keepUserTurns,
              beforeMessageId: currentDraft.beforeMessageId || undefined,
            },
            { loading: false, errorToast: false },
          )
          try {
            await loadWorkspaceChatHistory(snap.workspaceId, docStudioState.workspaceConfig || {})
          } catch (error) {
            // 服务端已回卷成功时，至少保证本地消息也被截断，避免 UI 与后端不一致。
            docStudioActions.truncateMessagesFromIndex(safeMsgIndex)
            message.warning(`会话已回卷，但刷新历史失败：${getErrorMessage(error)}`)
          }
        }

        if (restoreFiles) {
          if (!currentDraft.runId) {
            message.info('该消息无可用 checkpoint，已自动降级为仅清理后续对话')
          } else {
            try {
              const result = await restoreCheckpoint(
                { workspaceId: snap.workspaceId, runId: currentDraft.runId },
                { loading: false, errorToast: false },
              )
              const restoredFiles = result.restored_files || []
              for (const filePath of restoredFiles) {
                if (snap.openedFiles.includes(filePath)) {
                  await openFile(filePath, true, true)
                }
              }
              if (restoredFiles.length) {
                await loadWorkspaceFiles(snap.workspaceId, false)
              }
            } catch (error) {
              message.warning(`文件快照恢复失败：${getErrorMessage(error)}，已仅执行对话回卷`)
            }
          }
        }

        setReEditDraft(null)
        await handleSend({
          overridePrompt: submitPrompt,
          overrideImages: currentDraft.images,
          overrideSelections: currentDraft.selections,
          overrideFileMentions: currentDraft.fileMentions,
          useCurrentSelections: false,
          clearComposer: false,
        })
      } catch (error) {
        if ((error as Error)?.message === '__user_abort__') return
        message.error(`重编辑失败: ${getErrorMessage(error)}`)
      } finally {
        setReEditSubmitting(false)
      }
    },
    [
      chatLoading,
      confirmReEditRestoreAction,
      confirmOverwriteDirtyFiles,
      handleSend,
      loadWorkspaceChatHistory,
      loadWorkspaceFiles,
      openFile,
      reEditDraft,
      reEditSubmitting,
      snap.chatMessages,
      snap.openedFiles,
      snap.workspaceId,
    ],
  )

  const handleRevertOperation = async (operationId: string, files?: string[]) => {
    if (!snap.workspaceId) {
      message.warning('请先选择工作区')
      return
    }
    const targetFiles = Array.isArray(files) ? files.filter(Boolean) : []
    const scopedRevert = targetFiles.length > 0
    setRevertingOperationId(operationId)
    try {
      const result = await revertOperation(
        {
          workspaceId: snap.workspaceId,
          operationId,
          files: scopedRevert ? targetFiles : undefined,
        },
        {
          loading: false,
          errorToast: false,
        },
      )
      const revertedFiles = result.reverted_files || []
      for (const filePath of revertedFiles) {
        if (snap.openedFiles.includes(filePath)) {
          await openFile(filePath, true, true)
        }
      }
      if (result.deleted_files?.length) {
        await loadWorkspaceFiles(snap.workspaceId, false)
      }
      const affectedCount = (result.reverted_files?.length || 0) + (result.deleted_files?.length || 0)
      if (affectedCount) {
        message.success(
          scopedRevert
            ? `已恢复文件到该时间点（影响 ${affectedCount} 处变更）`
            : `已回滚 ${affectedCount} 个变更`,
        )
      } else {
        message.info(scopedRevert ? '该时间点没有可恢复内容' : '没有需要回滚的变更')
      }
      if (lastOperationId === operationId) {
        setLastOperationId(null)
      }
      await loadOperationHistory()
      if (diffModalOpen && diffModalContext === 'timeline') {
        closeDiffModal(
          allFileDiffs.map((d) => d.file_path).filter((p): p is string => Boolean(p)),
        )
      }
    } catch (error) {
      message.error(`回滚失败: ${getErrorMessage(error)}`)
    } finally {
      setRevertingOperationId(null)
    }
  }

  const closeDiffModal = useCallback(
    (pathsToRefresh?: string[], contentByPath?: Record<string, string>) => {
      const paths = pathsToRefresh && pathsToRefresh.length > 0 ? pathsToRefresh : []
      paths.forEach((p) => {
        const content = contentByPath?.[p]
        if (typeof content === 'string') {
          docStudioActions.setFileContent(p, content)
        } else {
          void openFile(p, true)
        }
      })
    const diffEditor = diffEditorRef.current
    const modifiedEditor =
      diffEditor && typeof diffEditor.getModifiedEditor === 'function'
        ? diffEditor.getModifiedEditor()
        : diffEditor
    if (modifiedEditor && typeof modifiedEditor.deltaDecorations === 'function') {
      diffHunkDecorationsRef.current = modifiedEditor.deltaDecorations(diffHunkDecorationsRef.current, [])
    }
    if (diffEditorListenerRef.current.length > 0) {
      diffEditorListenerRef.current.forEach((listener) => {
        try {
          listener.dispose()
        } catch (error) {
          // ignore dispose errors
        }
      })
      diffEditorListenerRef.current = []
    }
    setDiffModalOpen(false)
    setAgentDiffReviewOpen(false)
    setAllFileDiffs([])
    setCurrentDiffIndex(0)
    setDiffOperationId(null)
    setDiffModalContext('agent')
    setResolvedOriginal('')
    setResolvedModified('')
    setLineChanges([])
    setCurrentHunkIndex(0)
    },
    [openFile],
  )

  const openTimelineDiffPreview = useCallback((operationId: string, filePath?: string) => {
    const normalizedPath = normalizeWorkspacePath(String(filePath || ''))
    if (!operationId || !normalizedPath) {
      message.warning('缺少可预览的版本信息')
      return
    }
    setAgentDiffReviewOpen(false)
    setDiffModalContext('timeline')
    setDiffOperationId(operationId)
    setAllFileDiffs([
      {
        file_path: normalizedPath,
        original_content: '',
        modified_content: '',
      },
    ])
    setCurrentDiffIndex(0)
    setResolvedOriginal('')
    setResolvedModified('')
    setLineChanges([])
    setCurrentHunkIndex(0)
    setDiffModalOpen(true)
  }, [])

  const handleRejectCurrentDiff = async () => {
    if (!snap.workspaceId) {
      message.warning('请先选择工作区')
      return
    }
    const target = allFileDiffs[currentDiffIndex]
    if (!target?.file_path) {
      message.warning('请选择差异文件')
      return
    }
    if (!diffOperationId) {
      message.error('缺少变更 ID')
      return
    }
    if (diffReverting) return
    setDiffReverting(true)
    try {
      const result = await revertOperation(
        {
          workspaceId: snap.workspaceId,
          operationId: diffOperationId,
          files: [target.file_path],
        },
        {
          loading: false,
          errorToast: false,
        },
      )
      const revertedFiles = result.reverted_files || []
      for (const filePath of revertedFiles) {
        if (snap.openedFiles.includes(filePath)) {
          await openFile(filePath, true, true)
        }
      }
      if (result.deleted_files?.length) {
        await loadWorkspaceFiles(snap.workspaceId, false)
      }
      const affectedCount = (result.reverted_files?.length || 0) + (result.deleted_files?.length || 0)
      message.success(affectedCount ? `已回滚 ${affectedCount} 个变更` : '没有需要回滚的变更')

      const nextDiffs = allFileDiffs.filter((_, index) => index !== currentDiffIndex)
      if (!nextDiffs.length) {
        closeDiffModal()
      } else {
        setAllFileDiffs(nextDiffs)
        setCurrentDiffIndex(Math.min(currentDiffIndex, nextDiffs.length - 1))
      }
    } catch (error) {
      message.error(`回滚失败: ${getErrorMessage(error)}`)
    } finally {
      setDiffReverting(false)
    }
  }

  const handleKeepCurrentDiff = useCallback(
    (finalContent?: string) => {
      const currentPath = allFileDiffs[currentDiffIndex]?.file_path
      const paths = allFileDiffs.map((d) => d.file_path).filter((p): p is string => Boolean(p))
      const content =
        finalContent ??
        agentDiffReviewRef.current?.getCurrentModifiedContent() ??
        resolvedModified ??
        allFileDiffs[currentDiffIndex]?.modified_content
      const contentByPath = content && currentPath ? { [currentPath]: content } : undefined
    const nextDiffs = allFileDiffs.filter((_, index) => index !== currentDiffIndex)
    if (!nextDiffs.length) {
        closeDiffModal(paths, contentByPath)
    } else {
        if (contentByPath && currentPath) {
          docStudioActions.setFileContent(currentPath, content)
        }
      setAllFileDiffs(nextDiffs)
      setCurrentDiffIndex(Math.min(currentDiffIndex, nextDiffs.length - 1))
    }
    },
    [allFileDiffs, currentDiffIndex, closeDiffModal, resolvedModified],
  )

  const handleKeepAllDiffs = useCallback(() => {
    if (!allFileDiffs.length) {
      closeDiffModal()
      return
    }
    const paths = allFileDiffs.map((d) => d.file_path).filter((p): p is string => Boolean(p))
    const contentByPath: Record<string, string> = {}
    const currentModified =
      agentDiffReviewRef.current?.getCurrentModifiedContent() ?? resolvedModified
    allFileDiffs.forEach((d, idx) => {
      if (d.file_path) {
        contentByPath[d.file_path] =
          idx === currentDiffIndex && currentModified ? currentModified : (d.modified_content ?? '')
      }
    })
    closeDiffModal(paths, Object.keys(contentByPath).length > 0 ? contentByPath : undefined)
    message.success('已保留全部文件变更')
  }, [allFileDiffs, closeDiffModal, currentDiffIndex, resolvedModified])

  const handleRejectAllDiffs = async () => {
    if (!snap.workspaceId) {
      message.warning('请先选择工作区')
      return
    }
    if (!diffOperationId) {
      message.error('缺少变更 ID')
      return
    }
    const files = allFileDiffs
      .map((diff) => diff.file_path)
      .filter((path): path is string => Boolean(path))
    if (!files.length) {
      closeDiffModal()
      return
    }
    setDiffReverting(true)
    try {
      const result = await revertOperation(
        {
          workspaceId: snap.workspaceId,
          operationId: diffOperationId,
          files,
        },
        {
          loading: false,
          errorToast: false,
        },
      )
      const revertedFiles = result.reverted_files || []
      for (const filePath of revertedFiles) {
        if (snap.openedFiles.includes(filePath)) {
          await openFile(filePath, true, true)
        }
      }
      if (result.deleted_files?.length) {
        await loadWorkspaceFiles(snap.workspaceId, false)
      }
      const affectedCount = (result.reverted_files?.length || 0) + (result.deleted_files?.length || 0)
      message.success(affectedCount ? `已回滚 ${affectedCount} 个变更` : '没有需要回滚的变更')
      closeDiffModal(files)
    } catch (error) {
      message.error(`回滚失败: ${getErrorMessage(error)}`)
    } finally {
      setDiffReverting(false)
    }
  }

  const handleKeepHunk = useCallback(
    (hunkIndex: number) => {
      const change = lineChanges[hunkIndex]
      if (!change) return
      const orig = resolvedOriginal || allFileDiffs[currentDiffIndex]?.original_content || ''
      const mod =
        (agentDiffReviewRef.current?.getCurrentModifiedContent() ??
          resolvedModified ??
          allFileDiffs[currentDiffIndex]?.modified_content) ??
        ''
      const newOriginal = applyHunkKeep(change, orig, mod)
      setResolvedOriginal(newOriginal)
      setResolvedModified(mod)
      setCurrentHunkIndex(0)
      if (newOriginal === mod) {
        handleKeepCurrentDiff(newOriginal)
      }
    },
    [
      lineChanges,
      resolvedOriginal,
      resolvedModified,
      allFileDiffs,
      currentDiffIndex,
      handleKeepCurrentDiff,
    ],
  )

  const handleKeepCurrentHunk = useCallback(() => {
    if (!lineChanges.length) return
    handleKeepHunk(currentHunkIndex)
  }, [currentHunkIndex, lineChanges.length, handleKeepHunk])

  const applyLineChangeRevert = (
    change: any,
    originalText: string,
    modifiedText: string,
  ) => {
    const originalLines = originalText.split('\n')
    const modifiedLines = modifiedText.split('\n')
    const oStart = Number(change?.originalStartLineNumber || 0)
    const oEnd = Number(change?.originalEndLineNumber || 0)
    const mStart = Number(change?.modifiedStartLineNumber || 0)
    const mEnd = Number(change?.modifiedEndLineNumber || 0)

    const hasOriginal = oStart > 0 || oEnd > 0
    const hasModified = mStart > 0 || mEnd > 0
    const originalSlice = hasOriginal ? originalLines.slice(Math.max(oStart - 1, 0), oEnd) : []

    if (!hasOriginal && hasModified) {
      const start = Math.max(mStart - 1, 0)
      const count = Math.max(mEnd - mStart + 1, 0)
      modifiedLines.splice(start, count)
    } else if (hasOriginal && !hasModified) {
      const insertPos = Math.max((mStart || oStart) - 1, 0)
      modifiedLines.splice(insertPos, 0, ...originalSlice)
    } else {
      const start = Math.max(mStart - 1, 0)
      const count = Math.max(mEnd - mStart + 1, 0)
      modifiedLines.splice(start, count, ...originalSlice)
    }

    return modifiedLines.join('\n')
  }

  const applyHunkKeep = (
    change: any,
    originalText: string,
    modifiedText: string,
  ) => {
    const originalLines = originalText.split('\n')
    const modifiedLines = modifiedText.split('\n')
    const oStart = Number(change?.originalStartLineNumber || 0)
    const oEnd = Number(change?.originalEndLineNumber || 0)
    const mStart = Number(change?.modifiedStartLineNumber || 0)
    const mEnd = Number(change?.modifiedEndLineNumber || 0)
    const removeCount = oStart > 0 && oEnd >= oStart ? oEnd - oStart + 1 : 0
    const insertSlice =
      mStart > 0 && mEnd >= mStart
        ? modifiedLines.slice(Math.max(mStart - 1, 0), mEnd)
        : []
    const insertIdx = Math.max((oStart || 1) - 1, 0)
    originalLines.splice(insertIdx, removeCount, ...insertSlice)
    return originalLines.join('\n')
  }

  const handleRejectLineChange = async (changeIndex: number) => {
    if (!snap.workspaceId) {
      message.warning('请先选择工作区')
      return
    }
    const diff = allFileDiffs[currentDiffIndex]
    if (!diff?.file_path) {
      message.warning('请选择差异文件')
      return
    }
    const change = lineChanges[changeIndex]
    if (!change) {
      message.warning('未找到该变更')
      return
    }
    setDiffReverting(true)
    try {
      const nextContent = applyLineChangeRevert(change, resolvedOriginal, resolvedModified)
      await updateFileContent(
        {
          workspaceId: snap.workspaceId,
          path: diff.file_path,
          content: nextContent,
        },
        {
          loading: false,
          errorToast: false,
        },
      )
      docStudioActions.setFileContent(diff.file_path, nextContent)
      setResolvedModified(nextContent)
      setAllFileDiffs((prev) =>
        prev.map((item, idx) =>
          idx === currentDiffIndex
            ? { ...item, modified_content: nextContent, is_truncated: false }
            : item,
        ),
      )
      if (nextContent === resolvedOriginal) {
        const currentPath = diff.file_path
        const paths = allFileDiffs.map((d) => d.file_path).filter((p): p is string => Boolean(p))
        const contentByPath = currentPath ? { [currentPath]: nextContent } : undefined
        const nextDiffs = allFileDiffs.filter((_, index) => index !== currentDiffIndex)
        if (!nextDiffs.length) {
          closeDiffModal(paths, contentByPath)
        } else {
          if (currentPath) {
            docStudioActions.setFileContent(currentPath, nextContent)
          }
          setAllFileDiffs(nextDiffs)
          setCurrentDiffIndex(Math.min(currentDiffIndex, nextDiffs.length - 1))
        }
        message.success('该修改文件已全部撤销')
        return
      }
      message.success('已撤销该变更')
    } catch (error) {
      message.error(`更新失败: ${getErrorMessage(error)}`)
    } finally {
      setDiffReverting(false)
    }
  }

  const handleRejectCurrentHunk = useCallback(async () => {
    if (!lineChanges.length) return
    await handleRejectLineChange(currentHunkIndex)
  }, [currentHunkIndex, lineChanges.length, handleRejectLineChange])

  useEffect(() => {
    const reviewActive = agentDiffReviewOpen && diffModalContext === 'agent' && allFileDiffs.length > 0
    if (!reviewActive) return

    const handleReviewHotkeys = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (
        target &&
        (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
      ) {
        return
      }
      const lowerKey = event.key.toLowerCase()
      const withCtrl = event.ctrlKey || event.metaKey

      if (event.altKey && event.key === 'ArrowLeft') {
        event.preventDefault()
        setCurrentDiffIndex((index) => Math.max(index - 1, 0))
        return
      }
      if (event.altKey && event.key === 'ArrowRight') {
        event.preventDefault()
        setCurrentDiffIndex((index) => Math.min(index + 1, allFileDiffs.length - 1))
        return
      }
      if (withCtrl && event.shiftKey && lowerKey === 'y') {
        event.preventDefault()
        if (lineChanges.length > 0) {
          handleKeepCurrentHunk()
        } else {
          handleKeepCurrentDiff()
        }
        return
      }
      if (withCtrl && lowerKey === 'n') {
        event.preventDefault()
        if (lineChanges.length > 0) {
          void handleRejectCurrentHunk()
        } else {
          void handleRejectCurrentDiff()
        }
        return
      }
      if (event.altKey && event.key === 'ArrowUp') {
        event.preventDefault()
        setCurrentHunkIndex((index) => Math.max(index - 1, 0))
        return
      }
      if (event.altKey && event.key === 'ArrowDown') {
        event.preventDefault()
        setCurrentHunkIndex((index) => Math.min(index + 1, Math.max(lineChanges.length - 1, 0)))
      }
    }

    document.addEventListener('keydown', handleReviewHotkeys)
    return () => document.removeEventListener('keydown', handleReviewHotkeys)
  }, [
    agentDiffReviewOpen,
    allFileDiffs.length,
    diffModalContext,
    lineChanges.length,
    handleKeepCurrentHunk,
    handleKeepCurrentDiff,
    handleRejectCurrentHunk,
    handleRejectCurrentDiff,
  ])

  const closeAsyncStream = useCallback(() => {
    if (asyncStreamRef.current) {
      asyncStreamRef.current.close()
      asyncStreamRef.current = null
    }
  }, [])

  const pushChatMessage = useCallback((payload: Omit<DocStudioChatMessage, 'id' | 'createdAt'>) => {
    docStudioActions.appendChatMessage(payload)
  }, [])

  const resetLiveAgentPreview = useCallback(() => {
    setLiveAgentStatus('')
    setLiveAgentTimeline([])
    setLiveAgentPreviewText('')
    setLiveDeltaCharCount(0)
    setLiveAgentElapsedSec(0)
    liveDeltaStartedRef.current = false
    seenLiveEventIdsRef.current = new Set()
    handledInteractionIdsRef.current = new Set()
    lastLiveEventSequenceRef.current = -1
    activeRunIdRef.current = null
  }, [])

  const appendLiveTimelineEvent = useCallback((params: {
    text?: string
    eventType?: string
    level?: LiveTimelineLevel
    eventId?: string
    sequence?: number
    timestamp?: number
  }) => {
    const text = String(params.text || '').trim()
    if (!text) return
    const eventId = String(params.eventId || '').trim()
    const sequence = Number(params.sequence)
    if (eventId) {
      if (seenLiveEventIdsRef.current.has(eventId)) return
      seenLiveEventIdsRef.current.add(eventId)
    }
    if (Number.isFinite(sequence)) {
      if (sequence <= lastLiveEventSequenceRef.current) {
        return
      }
      lastLiveEventSequenceRef.current = sequence
    }
    const normalized: LiveTimelineEntry = {
      id: eventId || `local-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
      sequence: Number.isFinite(sequence) ? sequence : lastLiveEventSequenceRef.current + 1,
      eventType: params.eventType || 'log',
      text,
      level: params.level || 'info',
      timestamp: Number(params.timestamp) || Date.now(),
    }
    setLiveAgentTimeline((prev) => {
      const next = [...prev, normalized]
      return next.length > 120 ? next.slice(next.length - 120) : next
    })
  }, [])

  const appendLiveAgentEvent = useCallback((line?: string) => {
    const text = String(line || '').trim()
    if (!text) return
    appendLiveTimelineEvent({ text, eventType: 'log', level: 'info' })
  }, [appendLiveTimelineEvent])

  const summarizeStreamStep = useCallback((step: any) => {
    if (!step || typeof step !== 'object') return ''
    const type = String(step.type || '').toLowerCase()
    const tool = formatLiveToolName(step.tool || step.tool_name)
    const content = truncateLiveText(step.content, 96)
    const result =
      step.result && typeof step.result === 'object' ? (step.result as Record<string, any>) : undefined
    const durationSeconds = Number(result?.duration_seconds)
    const duration = Number.isFinite(durationSeconds) ? ` (${durationSeconds.toFixed(1)}s)` : ''
    const summary = typeof result?.summary === 'string' ? truncateLiveText(result.summary, 72) : ''
    const err = typeof result?.error === 'string' ? truncateLiveText(result.error, 72) : ''

    if (type === 'thought') return content ? `Thinking · ${content}` : 'Thinking...'
    if (type === 'reflection') return content ? `Reviewing · ${content}` : 'Reviewing...'
    if (type === 'action') {
      if (tool) return `调用工具 · ${tool}`
      return content ? `执行中 · ${content}` : '执行中...'
    }
    if (type === 'result') {
      const success = result?.success !== false
      if (tool) {
        if (success) {
          return summary ? `完成工具 · ${tool}${duration} · ${summary}` : `完成工具 · ${tool}${duration}`
        }
        return err ? `工具失败 · ${tool}${duration} · ${err}` : `工具失败 · ${tool}${duration}`
      }
      return content ? `结果 · ${content}` : ''
    }
    if (type === 'finish') return '正在生成最终回答...'
    if (type === 'start') return '任务已启动...'
    if (content) return content
    return ''
  }, [])

  const parseLiveEventMeta = useCallback((event: MessageEvent<string>, payload: any) => {
    const eventId = String(event.lastEventId || payload?.event_id || payload?.id || '').trim()
    const sequenceRaw = Number(payload?.sequence)
    const sequence = Number.isFinite(sequenceRaw) ? sequenceRaw : undefined
    const timestampRaw = Number(payload?.timestamp)
    const timestamp = Number.isFinite(timestampRaw)
      ? (timestampRaw > 1e12 ? Math.round(timestampRaw) : Math.round(timestampRaw * 1000))
      : Date.now()
    return { eventId, sequence, timestamp }
  }, [])

  const markPendingSendCommitted = useCallback((reason: 'delta' | 'tool') => {
    const pending = pendingSendRef.current
    if (!pending || pending.committed) return
    pending.committed = true
    pending.commitReason = reason
    docStudioActions.updateMessageMeta(pending.userMessageId, {
      streamCommitted: true,
      streamCommitReason: reason,
    })
  }, [])

  const rollbackPendingSendToComposer = useCallback(() => {
    const pending = pendingSendRef.current
    if (!pending || pending.committed) return false
    docStudioActions.removeChatMessageById(pending.userMessageId)
    setPrompt(pending.prompt)
    setSelections(normalizeSelectionFragments(pending.selections))
    setFileMentions(normalizeFileMentionFragments(pending.fileMentions))
    setChatImageAttachments(pending.images.map((item) => ({ ...item })))
    clearFileMentionSuggest()
    skipNextComposerClearRef.current = true
    pendingSendRef.current = null
    requestAnimationFrame(() => {
      promptInputDivRef.current?.focus()
    })
    return true
  }, [clearFileMentionSuggest])

  const handleStopSending = useCallback(async () => {
    if (!chatLoading) return
    stopRequestedRef.current = true
    const currentRunId = activeRunIdRef.current
    let cancelRequested = false
    if (snap.workspaceId && currentRunId) {
      try {
        await cancelAgentRun(
          { workspaceId: snap.workspaceId, runId: currentRunId },
          { loading: false, errorToast: false },
        )
        cancelRequested = true
        appendLiveTimelineEvent({
          text: '任务已取消',
          eventType: 'cancelled',
          level: 'warning',
        })
      } catch (error) {
        console.warn('Failed to cancel async run', error)
      }
    } else {
      cancelRequested = true
    }

    const pending = pendingSendRef.current
    const partialPreview = normalizeLiveDeltaText(liveAgentPreviewText).trim()
    const hasCommittedOutput = Boolean(pending?.committed)
    if (pending && !hasCommittedOutput && cancelRequested) {
      rollbackPendingSendToComposer()
      message.info('已撤回本次发送，可继续编辑后重发')
    } else {
      pendingSendRef.current = null
      if (partialPreview) {
        pushChatMessage({
          role: 'agent',
          content: `${partialPreview}\n\n[输出已中断，内容未完成]`,
          meta: {
            traceId: pending?.traceId,
            interrupted: true,
            partial: true,
          },
        })
      }
    }
    asyncRunResolvedRef.current = true
    resetLiveAgentPreview()
    closeAsyncStream()
    setChatLoading(false)
  }, [
    appendLiveTimelineEvent,
    chatLoading,
    closeAsyncStream,
    liveAgentPreviewText,
    pushChatMessage,
    resetLiveAgentPreview,
    rollbackPendingSendToComposer,
    snap.workspaceId,
  ])

  useEffect(() => {
    return () => {
      closeAsyncStream()
    }
  }, [closeAsyncStream])

  useEffect(() => {
    if (!asyncMode) {
      closeAsyncStream()
    }
  }, [asyncMode, closeAsyncStream])

  const handleAgentResponse = useCallback(
    async (response: DocStudioAPI.AgentResponse, traceId: string) => {
      pendingSendRef.current = null
      const changeCount = response.changes?.length || 0
      const operationId = response.operation_id || null
      const hasChanges = Boolean(
        (response.file_diffs && response.file_diffs.length > 0) ||
          (response.changes && response.changes.length > 0),
      )
      const isFileOpIntent = String(response.intent_type || '').toLowerCase() === 'file_op'
      pushChatMessage({
        role: 'agent',
        content: response.execution_history?.[response.execution_history.length - 1]?.content
          ? response.execution_history[response.execution_history.length - 1].content
          : `已执行变更 ${changeCount} 项`,
        meta: {
          changes: response.changes,
          traceId: response.trace_id || traceId,
          operationId,
        },
      })
      docStudioActions.setExecutionHistory(response.execution_history)
      docStudioActions.setAgentStatus({
        intentType: response.intent_type,
        intentConfidence: response.intent_confidence ?? undefined,
        plan: response.plan || undefined,
        warnings: response.warnings || [],
        traceId: response.trace_id || traceId,
        operationId,
      })
      if (operationId && hasChanges) {
        setLastOperationId(operationId)
      }
      // 文件操作（创建/移动/删除）不一定产生可视 diff，但必须立即同步左侧文件树。
      if ((hasChanges || isFileOpIntent) && snap.workspaceId) {
        await syncWorkspaceFileTree(snap.workspaceId)
      }
      if (operationId) {
        void loadOperationHistory()
      }
      if (response.file_diffs && response.file_diffs.length > 0) {
        setAllFileDiffs(response.file_diffs)
        setCurrentDiffIndex(0)
        setAgentDiffReviewOpen(true)
        setDiffModalContext('agent')
        setDiffOperationId(operationId)
        setDiffModalOpen(false)
      } else if (response.changes && response.changes.length > 0) {
        const affectedFiles = Array.from(
          new Set(
            (response.changes || [])
              .map((change) => change.file)
              .filter(Boolean) as string[],
          ),
        )
        for (const filePath of affectedFiles) {
          await openFile(filePath)
        }
        message.info(`已应用变更 ${changeCount} 项`)
      }
      refreshSystemStats(true)
      resetLiveAgentPreview()
      activeRunIdRef.current = null
      setChatLoading(false)
    },
    [
      loadOperationHistory,
      openFile,
      pushChatMessage,
      refreshSystemStats,
      resetLiveAgentPreview,
      snap.workspaceId,
      syncWorkspaceFileTree,
    ],
  )

  const handleFeedbackSubmit = useCallback(
    async (messageId: string, traceId: string | undefined, rating: DocStudioAPI.AgentFeedbackRating) => {
      if (!traceId) {
        message.warning('缺少 Trace ID，无法提交反馈')
      return
    }
      const target = snap.chatMessages.find((item) => item.id === messageId)
      if (target?.meta?.feedback === rating) {
        message.success('已提交反馈')
        return
      }
      setFeedbackSubmitting((prev) => ({ ...prev, [messageId]: true }))
      try {
        await sendAgentFeedback({ traceId, rating })
        docStudioActions.setMessageFeedback(messageId, rating)
        message.success('反馈已提交')
      } catch (error) {
        message.error(getErrorMessage(error))
      } finally {
        setFeedbackSubmitting((prev) => ({ ...prev, [messageId]: false }))
      }
    },
    [snap.chatMessages],
  )

  async function handleSend(options?: {
    overridePrompt?: string
    overrideImages?: ChatImageAttachment[]
    overrideSelections?: SelectionFragment[]
    overrideFileMentions?: FileMentionFragment[]
    useCurrentSelections?: boolean
    clearComposer?: boolean
  }) {
    const sourcePrompt = String(options?.overridePrompt ?? prompt)
    const sourceImages = Array.isArray(options?.overrideImages)
      ? options.overrideImages
      : chatImageAttachments
    const useCurrentSelections = options?.useCurrentSelections !== false
    const sourceSelections = Array.isArray(options?.overrideSelections)
      ? normalizeSelectionFragments(options.overrideSelections)
      : useCurrentSelections
        ? normalizeSelectionFragments(selections)
        : []
    const sourceFileMentions = Array.isArray(options?.overrideFileMentions)
      ? normalizeFileMentionFragments(options.overrideFileMentions)
      : normalizeFileMentionFragments(fileMentions)
    const clearComposer = options?.clearComposer !== false
    if (reEditDraft) {
      setReEditDraft(null)
    }
    if (!snap.workspaceId) {
      message.warning('请先选择工作区')
      return
    }
    if (!sourcePrompt.trim() && sourceImages.length === 0) {
      message.warning('请输入指令或添加图片')
      return
    }
    let activeSessionId =
      snap.workspaceConfig?.session_id ?? snap.workspaceConfig?.sessionId ?? null
    if (!snap.workspaceConfig?.session_id && !snap.workspaceConfig?.sessionId) {
      try {
        const { data: sessionRes } = await createSession(
          { ephemeral: true, surface: 'doc_studio' },
          { loading: false, errorToast: false },
        )
        const sessionId = sessionRes?.sessionId
        if (!sessionId) {
          message.error('创建会话失败，无法持久化对话')
          return
        }
        const detail = await bindWorkspaceSession(
          { workspaceId: snap.workspaceId, sessionId },
          { loading: false, errorToast: false },
        )
        docStudioActions.setWorkspaceConfig(detail.config)
        activeSessionId =
          detail?.config?.session_id ?? detail?.config?.sessionId ?? sessionId
      } catch (e: any) {
        const detail = e?.response?.data?.detail
        const status = e?.response?.status
        const errStr =
          typeof detail === 'string'
            ? detail
            : Array.isArray(detail)
              ? detail[0]?.msg ?? JSON.stringify(detail)
              : detail?.message ?? e?.message ?? String(e)
        console.error('[DocStudio] 创建并绑定会话失败:', {
          error: e,
          response: e?.response?.data,
          status,
        })
        const hint =
          status === 404 || status === 502
            ? '请确认主后端(8000)与 Doc Studio 服务已启动。'
            : errStr
        message.error(`创建并绑定会话失败：${hint}`)
        return
      }
    }
    const traceId =
      window.crypto?.randomUUID?.() ??
      `trace-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`
    
    let finalPrompt = sourcePrompt.trim() || '请结合图片内容回答我的问题。'
    const contextPayload: Record<string, any> = {}
    let linkedSelections: SelectionFragment[] = []
    let linkedFileMentions: FileMentionFragment[] = []
    
    if (snap.activeFilePath) {
      contextPayload.file_path = snap.activeFilePath
    }
    
    if (sourceSelections.length > 0) {
      const placeholdersInPrompt = new Set(finalPrompt.match(SELECTION_PLACEHOLDER_REGEX) || [])
      const selectionPairs = sourceSelections.map((sel, idx) => {
        const placeholder = normalizeSelectionPlaceholder(sel.placeholder, idx)
        const filePath = sel.filePath || snap.activeFilePath
        return {
          payload: {
            id: idx + 1,
            start: sel.start,
            end: sel.end,
            text: sel.text,
            preview: sel.text,
            total_chars: sel.totalChars ?? sel.text.length,
            is_range_reference: sel.isRangeReference ?? true,
            start_line: sel.startLine,
            end_line: sel.endLine,
            start_column: sel.startColumn,
            end_column: sel.endColumn,
            file_path: filePath,
            placeholder,
          },
          fragment: {
            id: sel.id || `${placeholder}-${sel.start}-${sel.end}-${idx}`,
            start: sel.start,
            end: sel.end,
            text: sel.text,
            filePath,
            placeholder,
            startLine: sel.startLine,
            endLine: sel.endLine,
            startColumn: sel.startColumn,
            endColumn: sel.endColumn,
            totalChars: sel.totalChars ?? sel.text.length,
            isRangeReference: sel.isRangeReference ?? true,
          } as SelectionFragment,
        }
      })
      const effectiveSelectionPairs = selectionPairs.filter((item) =>
        placeholdersInPrompt.has(item.payload.placeholder),
      )
      if (placeholdersInPrompt.size > 0 && effectiveSelectionPairs.length < placeholdersInPrompt.size) {
        const linked = new Set(effectiveSelectionPairs.map((item) => item.payload.placeholder))
        const missingCount = Array.from(placeholdersInPrompt).filter((ph) => !linked.has(ph)).length
        if (missingCount > 0) {
          message.warning(`检测到 ${missingCount} 个未绑定引用标签，将按普通文本处理`)
        }
      }
      if (effectiveSelectionPairs.length > 0) {
        const persistedSelections = effectiveSelectionPairs.map((item) => item.payload)
        contextPayload.selections = persistedSelections
        contextPayload.selection = {
          start: persistedSelections[0].start,
          end: persistedSelections[0].end,
          text: persistedSelections[0].text,
          preview: persistedSelections[0].preview,
          total_chars: persistedSelections[0].total_chars,
          is_range_reference: persistedSelections[0].is_range_reference,
          start_line: persistedSelections[0].start_line,
          end_line: persistedSelections[0].end_line,
          start_column: persistedSelections[0].start_column,
          end_column: persistedSelections[0].end_column,
          file_path: persistedSelections[0].file_path,
        }
        linkedSelections = effectiveSelectionPairs.map((item) => item.fragment)
      }
    } else if (containsSelectionPlaceholder(finalPrompt)) {
      message.warning('检测到选区标签但未绑定选区内容，将按普通文本处理')
    }
    if (sourceFileMentions.length > 0) {
      const filePlaceholdersInPrompt = new Set(finalPrompt.match(FILE_PLACEHOLDER_REGEX) || [])
      const mentionPairs = sourceFileMentions.map((mention, idx) => {
        const placeholder = normalizeFileMentionPlaceholder(mention.placeholder, idx)
        return {
          payload: {
            id: idx + 1,
            file_path: mention.filePath,
            placeholder,
            strategy: mention.strategy,
            total_chars: mention.totalChars,
            total_lines: mention.totalLines,
            file_hash: mention.fileHash,
            file_size: mention.fileSize,
          },
          mention: {
            ...mention,
            placeholder,
          } as FileMentionFragment,
        }
      })
      const effectiveMentionPairs = mentionPairs.filter((item) =>
        filePlaceholdersInPrompt.has(item.payload.placeholder),
      )
      if (filePlaceholdersInPrompt.size > 0 && effectiveMentionPairs.length < filePlaceholdersInPrompt.size) {
        const linked = new Set(effectiveMentionPairs.map((item) => item.payload.placeholder))
        const missingCount = Array.from(filePlaceholdersInPrompt).filter((ph) => !linked.has(ph)).length
        if (missingCount > 0) {
          message.warning(`检测到 ${missingCount} 个未绑定文件引用标签，将按普通文本处理`)
        }
      }
      if (effectiveMentionPairs.length > 0) {
        contextPayload.file_mentions = effectiveMentionPairs.map((item) => item.payload)
        linkedFileMentions = effectiveMentionPairs.map((item) => item.mention)
      }
    } else if (containsFilePlaceholder(finalPrompt)) {
      message.warning('检测到文件引用标签但未绑定文件内容，将按普通文本处理')
    }

    if (sourceImages.length > 0) {
      contextPayload.image_attachments = sourceImages.map((item) => ({
        name: item.name,
        mime_type: item.mimeType,
        size: item.size,
        data_url: item.dataUrl,
      }))
    }

    let effectiveModel: LlmModelValue = llmModel
    if (sourceImages.length > 0) {
      if (!isRuntimeVisionModel(effectiveModel)) {
        effectiveModel = defaultRuntimeVisionModelByProvider(resolveRuntimeProviderByModel(effectiveModel))
        message.info(`检测到图片输入，本次请求自动切换模型为 ${effectiveModel}`)
      }
    }
    const effectiveLlmOptions = {
      ...llmOptions,
      interaction_mode: interactionMode,
      llm_provider: resolveRuntimeProviderByModel(effectiveModel),
      llm_model: effectiveModel,
    }
    
    const userMessageText =
      sourceImages.length > 0
        ? `${finalPrompt}\n\n[已附带图片 ${sourceImages.length} 张]`
        : finalPrompt
    // Generate a stable id so we can back-patch runId after the run is created.
    const userMsgId = crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
    docStudioActions.appendChatMessage({
      id: userMsgId,
      role: 'user',
      content: userMessageText,
      meta: {
        traceId,
        imageCount: sourceImages.length || 0,
        images: sourceImages.map((item) => ({
          id: item.id,
          name: item.name,
          dataUrl: item.dataUrl,
          mimeType: item.mimeType,
          size: item.size,
        })),
        selectionCount: linkedSelections.length || 0,
        selections: linkedSelections,
        fileMentionCount: linkedFileMentions.length || 0,
        fileMentions: linkedFileMentions,
      },
    })
    pendingSendRef.current = {
      userMessageId: userMsgId,
      traceId,
      prompt: sourcePrompt,
      images: sourceImages.map((item) => ({ ...item })),
      selections: sourceSelections.map((item) => ({ ...item })),
      fileMentions: sourceFileMentions.map((item) => ({ ...item })),
      committed: false,
    }
    stopRequestedRef.current = false
    skipNextComposerClearRef.current = false
    const sid = activeSessionId
    if (sid) {
      const currentTitle = String(sessionTitlesRef.current[String(sid)] || '').trim()
      const nextTitle = buildSessionTitleFromPrompt(finalPrompt || userMessageText)
      if (nextTitle && isPlaceholderSessionTitle(currentTitle)) {
        sessionTitlesRef.current = {
          ...sessionTitlesRef.current,
          [sid]: nextTitle,
        }
        setSessionTitleVersion((value) => value + 1)
        autoTitledSessionRef.current[String(sid)] = true
        void renameSession(
          { sessionId: String(sid), sessionName: nextTitle },
          { loading: false, errorToast: false },
        ).catch(() => {})
      }
    }
    asyncRunResolvedRef.current = false
    setLiveAgentStatus('请求已接收，正在准备执行...')
    setLiveAgentTimeline([])
    setLiveAgentPreviewText('')
    setLiveDeltaCharCount(0)
    setLiveAgentElapsedSec(0)
    liveDeltaStartedRef.current = false
    seenLiveEventIdsRef.current = new Set()
    lastLiveEventSequenceRef.current = -1
    setChatLoading(true)
    try {
      docStudioActions.setAgentStatus({ intentType: undefined, plan: undefined, warnings: [] })
      const knowledgeBaseId = ragEnabled ? (selectedKnowledgeBaseId ?? undefined) : undefined
      const knowledgeBaseName = ragEnabled && knowledgeBaseId ? selectedKnowledgeBase?.name : undefined
      if (asyncMode) {
        closeAsyncStream()
        const asyncResult = await runAgentTaskAsync(
          {
            workspaceId: snap.workspaceId,
            userIntent: finalPrompt,
            context: Object.keys(contextPayload).length ? contextPayload : undefined,
            knowledgeBaseId,
            knowledgeBaseName,
            options: Object.keys(effectiveLlmOptions).length ? effectiveLlmOptions : undefined,
          },
          {
            headers: { 'X-Trace-Id': traceId },
            loading: false,
            errorToast: false,
          },
        )
        if (stopRequestedRef.current) {
          if (asyncResult?.runId && snap.workspaceId) {
            try {
              await cancelAgentRun(
                { workspaceId: snap.workspaceId, runId: asyncResult.runId },
                { loading: false, errorToast: false },
              )
            } catch (error) {
              console.warn('Failed to cancel async run after stop request', error)
            }
          }
          rollbackPendingSendToComposer()
          asyncRunResolvedRef.current = true
          activeRunIdRef.current = null
          setChatLoading(false)
          closeAsyncStream()
          return
        }
        if (!asyncResult.runId) {
          throw new Error('Failed to start async run')
        }
        activeRunIdRef.current = asyncResult.runId
        // Back-patch the user message with runId so "re-edit" can restore the checkpoint.
        docStudioActions.updateMessageMeta(userMsgId, { runId: asyncResult.runId })
        appendLiveAgentEvent(`任务已创建：${asyncResult.runId.slice(0, 8)}`)
        setLiveAgentStatus('任务已提交，等待执行...')
        // ScriptLens 适配：用 fetch+ReadableStream shim 替代 EventSource
        // （后者不支持 POST body / 自定义 Authorization header）
        const source = openAsyncEventStream(asyncResult.runId)
        let runtimeModelSwitchHandled = false
        asyncStreamRef.current = source

        source.addEventListener('start', (event) => {
          try {
            const messageEvent = event as MessageEvent<string>
            const payload = JSON.parse(messageEvent.data || '{}')
            const meta = parseLiveEventMeta(messageEvent, payload)
            const opId = String(payload?.operation_id || '')
            const mode = String(payload?.mode || '').toLowerCase()
            let text = '任务开始，正在分析需求...'
            if (opId) {
              text = `任务开始 · Op ${opId.slice(0, 8)}`
            }
            appendLiveTimelineEvent({
              text,
              eventType: 'start',
              level: 'info',
              eventId: meta.eventId,
              sequence: meta.sequence,
              timestamp: meta.timestamp,
            })
            if (mode === 'ask') {
              appendLiveTimelineEvent({
                text: '运行模式 · Ask',
                eventType: 'mode',
                level: 'info',
                eventId: meta.eventId ? `${meta.eventId}:mode` : undefined,
                timestamp: meta.timestamp,
              })
              setLiveAgentStatus('Ask 模式：正在生成回答...')
            } else {
              setLiveAgentStatus('正在分析需求...')
            }
          } catch (error) {
            console.warn('Failed to parse start event', error)
          }
        })

        source.addEventListener('delta', (event) => {
          try {
            const payload = JSON.parse((event as MessageEvent<string>).data || '{}')
            const delta = normalizeLiveDeltaText(payload?.delta)
            if (!delta) return
            markPendingSendCommitted('delta')
            setLiveAgentPreviewText((prev) => {
              const next = `${prev}${delta}`
              return next.length > 16000 ? next.slice(next.length - 16000) : next
            })
            setLiveDeltaCharCount((prev) => prev + delta.length)
            if (!liveDeltaStartedRef.current) {
              liveDeltaStartedRef.current = true
              setLiveAgentStatus('正在流式输出回答...')
              appendLiveTimelineEvent({
                text: '正在流式输出回答（下方灰色区域为实时文本预览）',
                eventType: 'delta_start',
                level: 'info',
              })
            }
          } catch (error) {
            console.warn('Failed to parse delta event', error)
          }
        })

        source.addEventListener('status', (event) => {
          try {
            const messageEvent = event as MessageEvent<string>
            const payload = JSON.parse(messageEvent.data || '{}')
            const meta = parseLiveEventMeta(messageEvent, payload)
            const status = String(payload?.status || '')
            if (status) {
              const statusMap: Record<string, string> = {
                queued: '任务已排队，等待执行...',
                running: '任务执行中...',
                awaiting_user_interaction: '等待你确认危险操作...',
                awaiting_confirmation: '等待你确认危险操作...',
                succeeded: '任务完成，正在整理结果...',
                failed: '任务执行失败',
                cancelled: '任务已取消',
              }
              const statusText = statusMap[status] || `任务状态：${status}`
              setLiveAgentStatus(statusText)
              appendLiveTimelineEvent({
                text: statusText,
                eventType: 'status',
                level: status === 'failed' ? 'error' : status === 'cancelled' ? 'warning' : 'info',
                eventId: meta.eventId,
                sequence: meta.sequence,
                timestamp: meta.timestamp,
              })
            }
            if (payload?.warning) {
              message.warning(payload.warning)
              appendLiveTimelineEvent({
                text: `警告：${payload.warning}`,
                eventType: 'status_warning',
                level: 'warning',
                eventId: meta.eventId ? `${meta.eventId}:warning` : undefined,
                timestamp: meta.timestamp,
              })
            }
          } catch (error) {
            console.warn('Failed to parse status event', error)
          }
        })
        source.addEventListener('runtime_model', (event) => {
          try {
            if (runtimeModelSwitchHandled) return
            const messageEvent = event as MessageEvent<string>
            const payload = JSON.parse(messageEvent.data || '{}')
            if (!payload?.fallback_applied) return
            const actualModel = String(payload.actual_model || '').trim()
            if (!actualModel) return
            runtimeModelSwitchHandled = true
            setLlmModel(actualModel)
            const requestedModel = String(payload.requested_model || llmModel || '').trim()
            const fromLabel = requestedModel ? resolveRuntimeModelLabel(requestedModel) : '所选模型'
            const toLabel = resolveRuntimeModelLabel(actualModel)
            message.warning(`所选模型不可用，已自动切换为真实使用模型：${fromLabel} → ${toLabel}`)
            appendLiveTimelineEvent({
              text: `模型已切换：${fromLabel} → ${toLabel}`,
              eventType: 'runtime_model',
              level: 'warning',
            })
          } catch (error) {
            console.warn('Failed to parse runtime_model event', error)
          }
        })
        source.addEventListener('plan', (event) => {
          try {
            const messageEvent = event as MessageEvent<string>
            const payload = JSON.parse(messageEvent.data || '{}')
            const meta = parseLiveEventMeta(messageEvent, payload)
            if (payload?.steps) {
              const planSteps = Array.isArray(payload.steps) ? payload.steps.length : 0
              if (planSteps > 0) {
                const text = `执行计划已生成：${planSteps} 步`
                appendLiveTimelineEvent({
                  text,
                  eventType: 'plan',
                  level: 'info',
                  eventId: meta.eventId,
                  sequence: meta.sequence,
                  timestamp: meta.timestamp,
                })
              }
              const current = docStudioState.agentStatus
              docStudioActions.setAgentStatus({
                intentType: current.intentType,
                intentConfidence: current.intentConfidence,
                warnings: current.warnings,
                traceId: current.traceId,
                operationId: current.operationId,
                plan: payload,
              })
            }
          } catch (error) {
            console.warn('Failed to parse plan event', error)
          }
        })
        source.addEventListener('step', (event) => {
          try {
            const messageEvent = event as MessageEvent<string>
            const payload = JSON.parse(messageEvent.data || '{}')
            const meta = parseLiveEventMeta(messageEvent, payload)
            if (payload?.step) {
              const preview = summarizeStreamStep(payload.step)
              if (preview) {
                const stepType = String(payload.step.type || '').toLowerCase()
                if (stepType === 'action') {
                  markPendingSendCommitted('tool')
                }
                appendLiveTimelineEvent({
                  text: preview,
                  eventType: `step_${stepType || 'unknown'}`,
                  level: stepType === 'error' ? 'error' : 'info',
                  eventId: meta.eventId,
                  sequence: meta.sequence,
                  timestamp: meta.timestamp,
                })
                if (stepType === 'action' || stepType === 'thought' || stepType === 'reflection') {
                  setLiveAgentStatus(preview)
                }
              }
              const next = [...docStudioState.executionHistory, payload.step]
              docStudioActions.setExecutionHistory(next)
            }
            if (payload?.plan) {
              const current = docStudioState.agentStatus
              docStudioActions.setAgentStatus({
                intentType: current.intentType,
                intentConfidence: current.intentConfidence,
                warnings: current.warnings,
                traceId: current.traceId,
                operationId: current.operationId,
                plan: payload.plan,
              })
            }
          } catch (error) {
            console.warn('Failed to parse step event', error)
          }
        })
        source.addEventListener('tool_call_start', (event) => {
          try {
            const messageEvent = event as MessageEvent<string>
            const payload = JSON.parse(messageEvent.data || '{}')
            const meta = parseLiveEventMeta(messageEvent, payload)
            markPendingSendCommitted('tool')
            const rawToolName = String(payload?.tool_name || '')
            const displayToolName = formatLiveToolName(rawToolName)
            const text = displayToolName ? `开始工具 · ${displayToolName}` : '开始工具调用'
            appendLiveTimelineEvent({
              text,
              eventType: 'tool_call_start',
              level: 'info',
              eventId: meta.eventId || String(payload?.tool_call_id || ''),
              sequence: meta.sequence,
              timestamp: meta.timestamp,
            })
            setLiveAgentStatus(text)
          } catch (error) {
            console.warn('Failed to parse tool_call_start event', error)
          }
        })
        source.addEventListener('tool_call_end', (event) => {
          try {
            const messageEvent = event as MessageEvent<string>
            const payload = JSON.parse(messageEvent.data || '{}')
            const meta = parseLiveEventMeta(messageEvent, payload)
            const rawToolName = String(payload?.tool_name || '')
            const displayToolName = formatLiveToolName(rawToolName)
            const ok = payload?.success !== false
            const durationRaw = Number(payload?.duration_seconds)
            const durationSuffix = Number.isFinite(durationRaw) ? ` (${durationRaw.toFixed(1)}s)` : ''
            const summary = truncateLiveText(
              ok ? String(payload?.summary || '') : String(payload?.error || ''),
              72,
            )
            const text = ok
              ? `完成工具 · ${displayToolName || rawToolName}${durationSuffix}${summary ? ` · ${summary}` : ''}`
              : `工具失败 · ${displayToolName || rawToolName}${durationSuffix}${summary ? ` · ${summary}` : ''}`
            appendLiveTimelineEvent({
              text,
              eventType: 'tool_call_end',
              level: ok ? 'info' : 'error',
              eventId: meta.eventId || String(payload?.tool_call_id || ''),
              sequence: meta.sequence,
              timestamp: meta.timestamp,
            })
          } catch (error) {
            console.warn('Failed to parse tool_call_end event', error)
          }
        })
        const handleInteractionRequired = (event: Event) => {
          try {
            const messageEvent = event as MessageEvent<string>
            const payload = JSON.parse(messageEvent.data || '{}')
            const meta = parseLiveEventMeta(messageEvent, payload)
            const interactionId = String(payload?.interaction_id || payload?.confirmation_id || '').trim()
            if (!interactionId) return
            if (handledInteractionIdsRef.current.has(interactionId)) return
            handledInteractionIdsRef.current.add(interactionId)

            markPendingSendCommitted('tool')
            const interactionType = String(payload?.interaction_type || 'dangerous_action_confirm')
            const toolName = formatLiveToolName(String(payload?.tool_name || 'delete_path_tool'))
            const targetPath = String(payload?.target_path || '')
            const recursive = Boolean(payload?.recursive)
            const preview =
              payload?.preview && typeof payload.preview === 'object'
                ? (payload.preview as Record<string, any>)
                : {}
            const samplePaths = Array.isArray(preview.sample_paths)
              ? preview.sample_paths
                  .map((item) => String(item || '').trim())
                  .filter(Boolean)
                  .slice(0, 6)
              : []
            const previewSummaryParts: string[] = []
            const fileSize = Number(preview.file_size_bytes)
            const fileCount = Number(preview.file_count)
            const dirCount = Number(preview.directory_count)
            if (Number.isFinite(fileSize) && fileSize > 0) previewSummaryParts.push(`文件大小 ${fileSize} B`)
            if (Number.isFinite(fileCount) && fileCount >= 0) previewSummaryParts.push(`文件数 ${fileCount}`)
            if (Number.isFinite(dirCount) && dirCount >= 0) previewSummaryParts.push(`目录数 ${dirCount}`)
            const summaryText = previewSummaryParts.join('，')
            const timelineText = targetPath
              ? `等待交互确认 · ${toolName} · ${targetPath}`
              : `等待交互确认 · ${toolName}`
            appendLiveTimelineEvent({
              text: timelineText,
              eventType: 'interaction_required',
              level: 'warning',
              eventId: meta.eventId,
              sequence: meta.sequence,
              timestamp: meta.timestamp,
            })
            setLiveAgentStatus('等待你确认危险操作...')

            if (interactionType !== 'dangerous_action_confirm') {
              appendLiveTimelineEvent({
                text: `暂不支持的交互类型：${interactionType}`,
                eventType: 'interaction_unsupported',
                level: 'error',
              })
              void respondAgentRunInteraction(
                {
                  workspaceId: snap.workspaceId,
                  runId: asyncResult.runId,
                  interactionId,
                  decision: 'reject',
                  note: `unsupported_interaction_type:${interactionType}`,
                },
                { loading: false, errorToast: false },
              ).catch((error) => {
                console.warn('Failed to auto reject unsupported interaction', error)
                handledInteractionIdsRef.current.delete(interactionId)
              })
              return
            }

            Modal.confirm({
              title: String(payload?.title || '确认危险操作'),
              okText: '确认执行',
              okType: 'danger',
              cancelText: '取消',
              maskClosable: false,
              content: (
                <div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>
                  <div>{`Agent 请求执行：${toolName}`}</div>
                  {targetPath ? <div>{`目标路径：${targetPath}`}</div> : null}
                  <div>{`递归删除：${recursive ? '是' : '否'}`}</div>
                  {summaryText ? <div>{`影响预览：${summaryText}`}</div> : null}
                  {samplePaths.length ? <div>{`样本路径：${samplePaths.join('，')}`}</div> : null}
                </div>
              ),
              onOk: async () => {
                try {
                  await respondAgentRunInteraction(
                    {
                      workspaceId: snap.workspaceId,
                      runId: asyncResult.runId,
                      interactionId,
                      decision: 'approve',
                    },
                    { loading: false, errorToast: false },
                  )
                  appendLiveTimelineEvent({
                    text: targetPath ? `你已确认执行危险操作：${targetPath}` : '你已确认执行危险操作',
                    eventType: 'interaction_user_approved',
                    level: 'warning',
                  })
                  setLiveAgentStatus('已确认，Agent 正在继续执行...')
                } catch (error) {
                  console.warn('Failed to submit approval decision', error)
                  message.error('提交确认失败，请重试')
                  handledInteractionIdsRef.current.delete(interactionId)
                  appendLiveTimelineEvent({
                    text: '确认提交失败，Agent 暂未收到决策',
                    eventType: 'interaction_submit_error',
                    level: 'error',
                  })
                }
              },
              onCancel: async () => {
                try {
                  await respondAgentRunInteraction(
                    {
                      workspaceId: snap.workspaceId,
                      runId: asyncResult.runId,
                      interactionId,
                      decision: 'reject',
                      note: 'user_rejected_from_modal',
                    },
                    { loading: false, errorToast: false },
                  )
                  appendLiveTimelineEvent({
                    text: targetPath ? `你已拒绝危险操作：${targetPath}` : '你已拒绝危险操作',
                    eventType: 'interaction_user_rejected',
                    level: 'info',
                  })
                  setLiveAgentStatus('已取消危险操作，Agent 将继续分析...')
                } catch (error) {
                  console.warn('Failed to submit rejection decision', error)
                  message.error('提交取消失败，请重试')
                  handledInteractionIdsRef.current.delete(interactionId)
                  appendLiveTimelineEvent({
                    text: '取消提交失败，Agent 暂未收到决策',
                    eventType: 'interaction_submit_error',
                    level: 'error',
                  })
                }
              },
            })
          } catch (error) {
            console.warn('Failed to handle interaction_required event', error)
          }
        }
        source.addEventListener('interaction_required', handleInteractionRequired)
        // Backward-compatible listener for old backend event name.
        source.addEventListener('confirmation_required', handleInteractionRequired)

        const handleInteractionResolved = (event: Event) => {
          try {
            const messageEvent = event as MessageEvent<string>
            const payload = JSON.parse(messageEvent.data || '{}')
            const meta = parseLiveEventMeta(messageEvent, payload)
            const decision = String(payload?.decision || '').toLowerCase()
            const text = decision === 'approve'
              ? '交互结果：已批准危险操作'
              : decision === 'reject'
                ? '交互结果：已拒绝危险操作'
                : `交互结果：${decision || 'unknown'}`
            appendLiveTimelineEvent({
              text,
              eventType: 'interaction_resolved',
              level: decision === 'approve' ? 'warning' : 'info',
              eventId: meta.eventId,
              sequence: meta.sequence,
              timestamp: meta.timestamp,
            })
          } catch (error) {
            console.warn('Failed to parse interaction_resolved event', error)
          }
        }
        source.addEventListener('interaction_resolved', handleInteractionResolved)
        // Backward-compatible listener for old backend event name.
        source.addEventListener('confirmation_resolved', handleInteractionResolved)
        source.addEventListener('cancelled', (event) => {
          try {
            const messageEvent = event as MessageEvent<string>
            const payload = JSON.parse(messageEvent.data || '{}')
            const meta = parseLiveEventMeta(messageEvent, payload)
            const reason = String(payload?.reason || 'cancelled_by_user')
            const text = `任务已取消 · ${reason}`
            setLiveAgentStatus('任务已取消')
            appendLiveTimelineEvent({
              text,
              eventType: 'cancelled',
              level: 'warning',
              eventId: meta.eventId,
              sequence: meta.sequence,
              timestamp: meta.timestamp,
            })
            asyncRunResolvedRef.current = true
            activeRunIdRef.current = null
            pendingSendRef.current = null
            setChatLoading(false)
            closeAsyncStream()
          } catch (error) {
            console.warn('Failed to parse cancelled event', error)
          }
        })
        source.addEventListener('finish', (event) => {
          try {
            const messageEvent = event as MessageEvent<string>
            const payload = JSON.parse(messageEvent.data || '{}')
            const meta = parseLiveEventMeta(messageEvent, payload)
            setLiveAgentStatus('正在生成最终回答...')
            appendLiveTimelineEvent({
              text: '正在生成最终回答...',
              eventType: 'finish',
              level: 'info',
              eventId: meta.eventId,
              sequence: meta.sequence,
              timestamp: meta.timestamp,
            })
            if (payload?.plan) {
              const current = docStudioState.agentStatus
              docStudioActions.setAgentStatus({
                intentType: current.intentType,
                intentConfidence: current.intentConfidence,
                warnings: current.warnings,
                traceId: current.traceId,
                operationId: current.operationId,
                plan: payload.plan,
              })
            }
          } catch (error) {
            console.warn('Failed to parse finish event', error)
          }
        })
        source.addEventListener('result', async (event) => {
          try {
            asyncRunResolvedRef.current = true
            const messageEvent = event as MessageEvent<string>
            const payload = JSON.parse(messageEvent.data || '{}')
            const meta = parseLiveEventMeta(messageEvent, payload)
            pendingSendRef.current = null
            if (payload?.result) {
              const runtimeModel = payload.result.runtime_model
              if (
                runtimeModel?.fallback_applied &&
                !runtimeModelSwitchHandled &&
                runtimeModel.actual_model
              ) {
                runtimeModelSwitchHandled = true
                const actualModel = String(runtimeModel.actual_model).trim()
                setLlmModel(actualModel)
                const requestedModel = String(runtimeModel.requested_model || llmModel || '').trim()
                const fromLabel = requestedModel ? resolveRuntimeModelLabel(requestedModel) : '所选模型'
                const toLabel = resolveRuntimeModelLabel(actualModel)
                message.warning(`所选模型不可用，已自动切换为真实使用模型：${fromLabel} → ${toLabel}`)
              }
              setLiveAgentStatus('结果已生成，正在应用变更...')
              appendLiveTimelineEvent({
                text: '结果已生成，正在应用变更...',
                eventType: 'result',
                level: 'info',
                eventId: meta.eventId,
                sequence: meta.sequence,
                timestamp: meta.timestamp,
              })
              await handleAgentResponse(payload.result, traceId)
            }
          } catch (error) {
            message.error('解析结果失败')
          } finally {
            closeAsyncStream()
          }
        })
        source.addEventListener('run_error', (event) => {
          try {
            asyncRunResolvedRef.current = true
            const messageEvent = event as MessageEvent<string>
            const payload = JSON.parse(messageEvent.data || '{}')
            const meta = parseLiveEventMeta(messageEvent, payload)
            const errorText = String(payload?.error || '执行失败')
            message.error(errorText)
            setLiveAgentStatus('任务执行失败')
            appendLiveTimelineEvent({
              text: `执行失败 · ${errorText}`,
              eventType: 'run_error',
              level: 'error',
              eventId: meta.eventId,
              sequence: meta.sequence,
              timestamp: meta.timestamp,
            })
          } catch (error) {
            message.error('执行失败')
          } finally {
            activeRunIdRef.current = null
            pendingSendRef.current = null
            setChatLoading(false)
            closeAsyncStream()
          }
        })
        source.onerror = async () => {
          if (asyncRunResolvedRef.current) {
            closeAsyncStream()
            return
          }
          setLiveAgentStatus('流式连接中断，正在恢复结果...')
          appendLiveTimelineEvent({
            text: '流式连接中断，尝试恢复运行结果...',
            eventType: 'sse_error',
            level: 'warning',
          })
          try {
            for (let attempt = 0; attempt < 10; attempt += 1) {
              // SSE 断连后，短轮询补偿最终结果，避免用户误以为“卡住”
              const snapshot = await fetchAgentRunStatus(
                {
                  workspaceId: snap.workspaceId,
                  runId: asyncResult.runId,
                },
                {
                  loading: false,
                  errorToast: false,
                },
              )
              if (snapshot?.status === 'succeeded' && snapshot?.result) {
                asyncRunResolvedRef.current = true
                appendLiveTimelineEvent({
                  text: '恢复成功，正在应用结果...',
                  eventType: 'recovery_succeeded',
                  level: 'info',
                })
                await handleAgentResponse(snapshot.result, traceId)
                closeAsyncStream()
                return
              }
              if (snapshot?.status === 'cancelled') {
                asyncRunResolvedRef.current = true
                setLiveAgentStatus('任务已取消')
                appendLiveTimelineEvent({
                  text: '任务已取消',
                  eventType: 'cancelled',
                  level: 'warning',
                })
                activeRunIdRef.current = null
                pendingSendRef.current = null
                setChatLoading(false)
                closeAsyncStream()
                return
              }
              if (snapshot?.status === 'failed') {
                asyncRunResolvedRef.current = true
                const errorText = snapshot?.error || '执行失败'
                message.error(errorText)
                setLiveAgentStatus('任务执行失败')
                appendLiveTimelineEvent({
                  text: `执行失败 · ${errorText}`,
                  eventType: 'recovery_failed',
                  level: 'error',
                })
                activeRunIdRef.current = null
                pendingSendRef.current = null
                setChatLoading(false)
                closeAsyncStream()
                return
              }
              await new Promise((resolve) => window.setTimeout(resolve, 1200))
            }
          } catch (error) {
            console.warn('Failed to recover async run result', error)
          }
          setLiveAgentStatus('连接恢复失败，请重试')
          appendLiveTimelineEvent({
            text: '连接恢复失败，请重试',
            eventType: 'recovery_timeout',
            level: 'error',
          })
          activeRunIdRef.current = null
          pendingSendRef.current = null
          setChatLoading(false)
          closeAsyncStream()
        }
        return
      }

      const response = await runAgentTask(
        {
          workspaceId: snap.workspaceId,
          userIntent: finalPrompt,
          context: Object.keys(contextPayload).length ? contextPayload : undefined,
          knowledgeBaseId,
          knowledgeBaseName,
          options: Object.keys(effectiveLlmOptions).length ? effectiveLlmOptions : undefined,
        },
        {
          headers: { 'X-Trace-Id': traceId },
          loading: false,
          errorToast: false,
        },
      )
      await handleAgentResponse(response, traceId)
    } catch (error) {
      if (stopRequestedRef.current) {
        rollbackPendingSendToComposer()
      } else {
        showRequestError(error)
      }
      if (!stopRequestedRef.current) {
        pendingSendRef.current = null
      }
      resetLiveAgentPreview()
      setChatLoading(false)
    } finally {
      const skipComposerClear = skipNextComposerClearRef.current
      if (skipComposerClear) {
        skipNextComposerClearRef.current = false
      }
      if (clearComposer && !skipComposerClear) {
        setPrompt('')
        setSelections([])
        setFileMentions([])
        clearFileMentionSuggest()
        setChatImageAttachments([])
      }
      if (!asyncMode) {
        setChatLoading(false)
      }
      stopRequestedRef.current = false
    }
  }

  const contextMenuActions: ContextMenuAction[] = (() => {
    const closeContextMenu = () => setContextMenuVisible(false)
    const isProtectedDirectory =
      contextMenuType === 'directory' &&
      isNotebookSystemPath(contextMenuPath, { protectParents: true })
    const isProtectedFile =
      contextMenuType === 'file' &&
      isNotebookSystemPath(contextMenuPath)

    const confirmDeleteDirectory = () => {
      closeContextMenu()
      Modal.confirm({
        title: '确认删除文件夹？',
        content: `确定删除文件夹 "${contextMenuPath}" 及其所有内容？`,
        okText: '删除',
        okType: 'danger',
        cancelText: '取消',
        onOk: () => handleDeleteFromTree(contextMenuPath, 'directory'),
      })
    }

    const confirmDeleteFile = () => {
      closeContextMenu()
      Modal.confirm({
        title: '确认删除文件？',
        content: `确定删除文件 "${contextMenuPath}"？`,
        okText: '删除',
        okType: 'danger',
        cancelText: '取消',
        onOk: () => handleDeleteFromTree(contextMenuPath, 'file'),
      })
    }

    if (contextMenuType === 'workspace') {
      return [
        {
          key: 'workspace-new-file',
          label: '新建文件',
          icon: <FileAddOutlined />,
          onClick: () => {
            closeContextMenu()
            openCreateModalAtPath('file')
          },
        },
        {
          key: 'workspace-new-folder',
          label: '新建文件夹',
          icon: <FolderAddOutlined />,
          onClick: () => {
            closeContextMenu()
            openCreateModalAtPath('directory')
          },
        },
        {
          key: 'workspace-upload',
          label: '上传文件',
          icon: <UploadOutlined />,
          onClick: () => {
            closeContextMenu()
            handleWorkspaceUploadClick()
          },
        },
        {
          key: 'workspace-refresh',
          label: '刷新文件树',
          icon: <ReloadOutlined />,
          onClick: () => {
            closeContextMenu()
            void refreshFileTree(true)
          },
        },
      ]
    }

    if (contextMenuType === 'directory') {
      if (isProtectedDirectory) {
        return [
          {
            key: 'directory-readonly',
            label: '系统目录（结构保护）',
            icon: <FolderOpenOutlined />,
            disabled: true,
            onClick: closeContextMenu,
          },
          {
            key: 'directory-refresh',
            label: '刷新文件树',
            icon: <ReloadOutlined />,
            onClick: () => {
              closeContextMenu()
              void refreshFileTree(true)
            },
          },
        ]
      }
      return [
        {
          key: 'directory-rename',
          label: '重命名',
          icon: <EditOutlined />,
          onClick: () => {
            closeContextMenu()
            openRenameModal(contextMenuPath, 'directory')
          },
        },
        {
          key: 'directory-new-file',
          label: '新建文件',
          icon: <FileAddOutlined />,
          onClick: () => {
            closeContextMenu()
            handleCreateFileInDirectory(contextMenuPath)
          },
        },
        {
          key: 'directory-new-folder',
          label: '新建文件夹',
          icon: <FolderAddOutlined />,
          onClick: () => {
            closeContextMenu()
            handleCreateFolderInDirectory(contextMenuPath)
          },
        },
        {
          key: 'directory-upload',
          label: '上传文件',
          icon: <UploadOutlined />,
          onClick: () => {
            closeContextMenu()
            handleUploadToDirectory(contextMenuPath)
          },
        },
        {
          key: 'directory-delete',
          label: '删除文件夹',
          icon: <DeleteOutlined />,
          danger: true,
          separated: true,
          onClick: confirmDeleteDirectory,
        },
      ]
    }

    if (isProtectedFile) {
      return [
        {
          key: 'file-download',
          label: '下载文件',
          icon: <DownloadOutlined />,
          onClick: () => {
            closeContextMenu()
            void handleDownloadFileAtPath(contextMenuPath)
          },
        },
        {
          key: 'file-delete',
          label: '删除文件',
          icon: <DeleteOutlined />,
          danger: true,
          separated: true,
          onClick: confirmDeleteFile,
        },
      ]
    }

    return [
      {
        key: 'file-rename',
        label: '重命名',
        icon: <EditOutlined />,
        onClick: () => {
          closeContextMenu()
          openRenameModal(contextMenuPath, 'file')
        },
      },
      {
        key: 'file-download',
        label: '下载文件',
        icon: <DownloadOutlined />,
        onClick: () => {
          closeContextMenu()
          void handleDownloadFileAtPath(contextMenuPath)
        },
      },
      {
        key: 'file-delete',
        label: '删除文件',
        icon: <DeleteOutlined />,
        danger: true,
        separated: true,
        onClick: confirmDeleteFile,
      },
    ]
  })()

  const headerOverflowMenuItems = useMemo<MenuProps['items']>(() => {
    const items: MenuProps['items'] = [
      {
        key: 'download',
        label: '下载当前文件',
        icon: <DownloadOutlined />,
        disabled: !snap.activeFilePath,
      },
      {
        key: 'delete',
        label: '删除当前文件',
        icon: <DeleteOutlined />,
        disabled: !snap.activeFilePath,
        danger: true,
      },
    ]
    if (lastOperationId && !undoingLastApply) {
      items.push({
        key: 'undo',
        label: '撤销应用',
        icon: <SyncOutlined />,
      })
    }
    items.push({
      key: 'history',
      label: '文件时间线',
      icon: <HistoryOutlined />,
    })
    return items
  }, [lastOperationId, snap.activeFilePath, undoingLastApply])

  const handleHeaderOverflowMenuClick = useCallback<NonNullable<MenuProps['onClick']>>(
    ({ key }) => {
      if (key === 'download') {
        void handleDownloadCurrentFile()
        return
      }
      if (key === 'delete') {
        if (snap.activeFilePath) {
          showDeleteConfirm(snap.activeFilePath, 'file')
        }
        return
      }
      if (key === 'undo') {
        void handleUndoLastApply()
        return
      }
      if (key === 'history') {
        setRightTab('history')
      }
    },
    [handleDownloadCurrentFile, handleUndoLastApply, showDeleteConfirm, snap.activeFilePath],
  )

  const renderTreeNodeTitle = useCallback(
    (node: ReadonlyFileNode) => {
      const isHovered = hoveredTreePath === node.path
      const deleteLabel = node.type === 'directory' ? '删除文件夹' : '删除文件'
      const isProtectedDirectory =
        node.type === 'directory' && isNotebookSystemPath(node.path, { protectParents: true })
      const isProtectedFile = node.type === 'file' && isNotebookSystemPath(node.path)
      return (
        <span
          className="doc-studio__tree-node"
          onMouseEnter={() => setHoveredTreePath(node.path)}
          onMouseLeave={() => {
            setHoveredTreePath((prev) => (prev === node.path ? '' : prev))
          }}
        >
          <span className="doc-studio__tree-node-main">
            {node.type === 'directory' ? (
              <FolderOpenOutlined className="doc-studio__tree-node-icon doc-studio__tree-node-icon--directory" />
            ) : (
              <FileTextOutlined className="doc-studio__tree-node-icon doc-studio__tree-node-icon--file" />
            )}
            <span className="doc-studio__tree-node-name" title={node.name}>
              {node.name}
            </span>
          </span>
          {isHovered && !isProtectedDirectory && (
            <span className="doc-studio__tree-node-actions">
              {!isProtectedFile && (
                <Tooltip title="重命名（F2）">
                  <Button
                    type="text"
                    className="doc-studio__tree-action-btn"
                    icon={<EditOutlined />}
                    onMouseDown={(event) => {
                      event.preventDefault()
                      event.stopPropagation()
                    }}
                    onClick={(event) => {
                      event.preventDefault()
                      event.stopPropagation()
                      openRenameModal(node.path, node.type)
                    }}
                  />
                </Tooltip>
              )}
              <Tooltip title={deleteLabel}>
                <Button
                  type="text"
                  className="doc-studio__tree-action-btn doc-studio__tree-action-btn--danger"
                  icon={<DeleteOutlined />}
                  onMouseDown={(event) => {
                    event.preventDefault()
                    event.stopPropagation()
                  }}
                  onClick={(event) => {
                    event.preventDefault()
                    event.stopPropagation()
                    showDeleteConfirm(node.path, node.type)
                  }}
                />
              </Tooltip>
            </span>
          )}
        </span>
      )
    },
    [hoveredTreePath, isNotebookSystemPath, openRenameModal, showDeleteConfirm],
  )

  const treeData = useMemo(
    () => buildTreeData(cloneFileNodes(snap.fileTree), renderTreeNodeTitle),
    [renderTreeNodeTitle, snap.fileTree],
  )

  const isAgentDiffReviewActive =
    agentDiffReviewOpen && diffModalContext === 'agent' && allFileDiffs.length > 0
  const hasPendingAgentReview =
    diffModalContext === 'agent' && allFileDiffs.length > 0
  const currentReviewDiff = isAgentDiffReviewActive ? allFileDiffs[currentDiffIndex] : undefined

  return (
    <>
    <div className="doc-studio-page">
        <Layout className={`doc-studio ${isDraggingLeft || isDraggingRight ? 'doc-studio--resizing' : ''}`}>
          <Sider
            width={leftPanelClosed ? 0 : leftSiderWidth}
            className={`doc-studio__sider ${leftPanelClosed ? 'doc-studio__sider--collapsed' : ''}`}
          >
            <div className="doc-studio__workspace">
              <Select
                value={snap.workspaceId || undefined}
                className="doc-studio__workspace-select"
                placeholder="选择工作区"
                options={workspaceOptions}
                loading={snap.workspaceLoading}
                onChange={handleWorkspaceChange}
              />
              <Space size="small">
                <Button
                  icon={<PlusOutlined />}
                  size="small"
                  onClick={() => setWorkspaceModalOpen(true)}
                />
                <Button
                  icon={<ReloadOutlined />}
                  size="small"
                  onClick={() => loadWorkspaces(snap.workspaceId)}
                />
                {/* ScriptLens 扩展：跳转到 5 维分析报告页（PRD §三-4 决策卡 + 三视角） */}
                <Tooltip title={snap.workspaceId ? '查看 5 维分析报告' : '请先选择剧本'}>
                  <Button
                    icon={<BarChartOutlined />}
                    size="small"
                    disabled={!snap.workspaceId}
                    onClick={() => {
                      if (snap.workspaceId) {
                        navigate(`/scripts/${snap.workspaceId}/report`)
                      }
                    }}
                  />
                </Tooltip>
              </Space>
            </div>
            <div className="doc-studio__explorer-header" onContextMenu={handleExplorerContextMenu}>
              <span className="doc-studio__explorer-name" title={activeWorkspaceName}>
                {explorerTitle}
              </span>
              <div className="doc-studio__explorer-actions">
                <Tooltip title="新建文件">
                  <Button
                    type="text"
                    className="doc-studio__explorer-action-btn"
                    icon={<FileAddOutlined />}
                    onClick={() => openFileModal('file')}
                    disabled={!snap.workspaceId}
                  />
                </Tooltip>
                <Tooltip title="新建文件夹">
                  <Button
                    type="text"
                    className="doc-studio__explorer-action-btn"
                    icon={<FolderAddOutlined />}
                    onClick={() => openFileModal('directory')}
                    disabled={!snap.workspaceId}
                  />
                </Tooltip>
                <Tooltip title="上传文件">
                  <Button
                    type="text"
                    className="doc-studio__explorer-action-btn"
                    icon={<UploadOutlined />}
                    loading={uploading}
                    onClick={handleWorkspaceUploadClick}
                    disabled={!snap.workspaceId}
                  />
                </Tooltip>
                <Tooltip title="刷新文件树">
                  <Button
                    type="text"
                    className="doc-studio__explorer-action-btn"
                    icon={<ReloadOutlined />}
                    disabled={!snap.workspaceId}
                    onClick={() => refreshFileTree(true)}
                  />
                </Tooltip>
                <Tooltip title="折叠文件栏 (Ctrl+B)">
                  <Button
                    type="text"
                    className="doc-studio__explorer-action-btn"
                    icon={<MenuFoldOutlined />}
                    onClick={() => setLeftPanelClosed(true)}
                  />
                </Tooltip>
              </div>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              style={{ display: 'none' }}
              onChange={handleFileInputChange}
            />
            <div className="doc-studio__tree-wrapper" onContextMenu={handleExplorerContextMenu}>
              {treeData.length ? (
                <Tree
                  selectedKeys={snap.activeFilePath ? [snap.activeFilePath] : []}
                  expandedKeys={expandedKeys}
                  onExpand={(keys) => setExpandedKeys(keys)}
                  treeData={treeData}
                  onSelect={handleTreeSelect}
                  onRightClick={handleRightClick}
                />
              ) : (
                <Empty
                  description="暂无文件"
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                />
              )}
            </div>
          </Sider>
          {!leftPanelClosed && (
          <div
            className={`doc-studio__resizer doc-studio__resizer--left ${isDraggingLeft ? 'doc-studio__resizer--dragging' : ''}`}
            onMouseDown={handleLeftResizeStart}
          />
          )}
          <Layout className="doc-studio__center">
            <Header className="doc-studio__header">
              <div className="doc-studio__header-main">
                <div className="doc-studio__header-tabs-wrap">
                  {snap.openedFiles.length ? (
                    <Tabs
                      className="doc-studio__header-tabs"
                      type="editable-card"
                      hideAdd
                      size="small"
                      activeKey={snap.activeFilePath || undefined}
                      onChange={handleTabChange}
                      onEdit={handleTabEdit}
                      items={headerTabItems}
                    />
                  ) : (
                    <span className="doc-studio__header-empty">未打开文件</span>
                  )}
                </div>
                <div className="doc-studio__header-bar">
                  <Space size={6} className="doc-studio__header-actions">
                    {leftPanelClosed && (
                      <Tooltip title="展开文件栏 (Ctrl+B)">
                        <Button
                          type="text"
                          className="doc-studio__header-icon-btn"
                          icon={<MenuOutlined />}
                          onClick={() => setLeftPanelClosed(false)}
                        />
                      </Tooltip>
                    )}
                    {rightPanelClosed && (
                      <Tooltip title="展开对话 (Ctrl+L)">
                        <Button
                          type="text"
                          className="doc-studio__header-icon-btn"
                          icon={<MessageOutlined />}
                          onClick={() => {
                            setRightPanelClosed(false)
                            setRightTab('chat')
                          }}
                        />
                      </Tooltip>
                    )}
                    {supportsCompilePanel && (
                      <Tooltip title={compileActionTitle}>
                        <Button
                          type="text"
                          className="doc-studio__header-icon-btn doc-studio__header-icon-btn--primary"
                          icon={<PlayCircleOutlined />}
                          onClick={handleCompile}
                          disabled={!snap.workspaceId || isPlaintextActiveFile}
                        />
                      </Tooltip>
                    )}
                    <Dropdown
                      trigger={['click']}
                      placement="bottomRight"
                      menu={{
                        items: headerOverflowMenuItems,
                        onClick: handleHeaderOverflowMenuClick,
                      }}
                    >
                      <Button
                        type="text"
                        className="doc-studio__header-overflow-btn"
                        icon={<EllipsisOutlined />}
                      />
                    </Dropdown>
                  </Space>
                </div>
              </div>
            </Header>
            <Content className="doc-studio__content">
              {snap.openedFiles.length || isAgentDiffReviewActive ? (
                <div className="doc-studio__editor-wrapper">
                  {isAgentDiffReviewActive && currentReviewDiff ? (
                    <div className="doc-studio__review-shell">
                      <div className="doc-studio__review-toolbar">
                        <Space size={8} wrap>
                          <Tag color="blue">Agent 变更审阅</Tag>
                          <Text type="secondary">{currentDiffIndex + 1} of {allFileDiffs.length} files</Text>
                          <Text code className="doc-studio__review-file-tag">
                            {currentReviewDiff.file_path || '-'}
                          </Text>
                          <Tag color="green">+{normalizeCount(currentReviewDiff.added_lines)}</Tag>
                          <Tag color="volcano">-{normalizeCount(currentReviewDiff.removed_lines)}</Tag>
                          {currentReviewDiff.is_truncated && <Tag color="orange">已截断</Tag>}
                        </Space>
                        <Space size={8} wrap>
                          <Button
                            size="small"
                            disabled={currentDiffIndex === 0}
                            onClick={() => setCurrentDiffIndex((index) => Math.max(index - 1, 0))}
                          >
                            上一文件
                          </Button>
                          <Button
                            size="small"
                            disabled={currentDiffIndex >= allFileDiffs.length - 1}
                            onClick={() =>
                              setCurrentDiffIndex((index) => Math.min(index + 1, allFileDiffs.length - 1))
                            }
                          >
                            下一文件
                          </Button>
                          {lineChanges.length > 0 ? (
                            <Tag>{lineChanges.length} 处修改点</Tag>
                          ) : (
                            <Tag>无修改点</Tag>
                          )}
                          <Button
                            size="small"
                            danger
                            disabled={diffReverting}
                            onClick={handleRejectCurrentDiff}
                            title="Undo File (Ctrl/Cmd+N)"
                          >
                            Undo File
                          </Button>
                          <Button
                            size="small"
                            type="primary"
                            onMouseDown={() => (document.activeElement as HTMLElement)?.blur?.()}
                            onClick={() => handleKeepCurrentDiff()}
                            title="Keep File (Ctrl/Cmd+Shift+Y)"
                          >
                            Keep File
                          </Button>
                        </Space>
                      </div>
                      <div className="doc-studio__review-main">
                        <div className="doc-studio__review-content">
                          <div className="doc-studio__review-diff">
                              <AgentDiffReview
                                ref={agentDiffReviewRef}
                                key={`agent-review-${currentReviewDiff.file_path}-${currentDiffIndex}`}
                                filePath={currentReviewDiff.file_path || ''}
                                originalContent={resolvedOriginal || currentReviewDiff.original_content}
                                modifiedContent={resolvedModified || currentReviewDiff.modified_content}
                                diffReverting={diffReverting}
                                currentHunkIndex={currentHunkIndex}
                                onModifiedContentChange={(next) => setResolvedModified(next)}
                                onHunkUndo={(idx) => void handleRejectLineChange(idx)}
                                onHunkKeep={(idx) => handleKeepHunk(idx)}
                                onLineChangesReady={(changes) => {
                                  setLineChanges(changes)
                                  setCurrentHunkIndex((prev) => Math.min(prev, Math.max(0, changes.length - 1)))
                                }}
                              />
                          </div>
                        </div>
                      </div>
                    </div>
                  ) : null}
                  <div
                    style={{
                      flex: 1,
                      overflow: 'hidden',
                      minHeight: 0,
                      display: isAgentDiffReviewActive && currentReviewDiff ? 'none' : 'block',
                    }}
                  >
                      {snap.activeFilePath ? (
                        <Editor
                          key={snap.activeFilePath}
                          theme="vs-dark"
                          height="100%"
                          language={resolveEditorLanguage(snap.activeFilePath)}
                          loading={<Spin />}
                          value={currentFileBuffer?.content || ''}
                          onChange={handleEditorChange}
                          onMount={handleEditorMount}
                          options={{
                            readOnly: currentFileBuffer?.loading,
                            minimap: { enabled: false },
                            fontSize: 14,
                            wordWrap: 'on',
                            automaticLayout: true,
                            selectOnLineNumbers: true,
                            scrollBeyondLastLine: false,
                            // ???????????????Ctrl+A, Ctrl+C, Ctrl+V, Ctrl+Z ??
                            // Monaco Editor ?????????????????
                          }}
                        />
                      ) : (
                        // 加载工作区时不显示引导，避免刷新时闪烁
                        snap.workspaceLoading ? null : <DocStudioWelcome />
                      )}
                    </div>
                </div>
              ) : (
                snap.workspaceLoading ? null : <DocStudioWelcome />
              )}
            </Content>
          </Layout>
          {!rightPanelClosed && (
            <>
          <div
            className={`doc-studio__resizer doc-studio__resizer--right ${isDraggingRight ? 'doc-studio__resizer--dragging' : ''}`}
            onMouseDown={handleRightResizeStart}
          />
          <Sider width={rightSiderWidth} className="doc-studio__right">
            <div className="doc-studio__right-inner">
            {/* 右侧 Tab 导航（自定义样式，非 Ant Design Tabs） */}
            <div className="doc-studio__custom-tabs">
              <div className="doc-studio__custom-tabs-nav">
                <button
                  className={`doc-studio__custom-tab ${rightTab === 'chat' ? 'doc-studio__custom-tab--active' : ''}`}
                  onClick={() => setRightTab('chat')}
                >
                  Agent 对话
                </button>
                <button
                  className={`doc-studio__custom-tab ${rightTab === 'history' ? 'doc-studio__custom-tab--active' : ''}`}
                  onClick={() => setRightTab('history')}
                >
                  时间线
                </button>
                {supportsCompilePanel && (
                  <button
                    className={`doc-studio__custom-tab ${rightTab === 'compile' ? 'doc-studio__custom-tab--active' : ''}`}
                    onClick={() => setRightTab('compile')}
                  >
                    编译结果
                  </button>
                )}
              </div>
              <div className="doc-studio__custom-tabs-content">
                {/* Chat Panel */}
                {rightTab === 'chat' && (
                <div className="doc-studio__chat-panel">
                      {/* Cursor 风格对话顶部栏：+ 新建、对话标签、历史、更多、关闭 */}
                      <div className="doc-studio__chat-header">
                        <div className="doc-studio__chat-header-tabs">
                          {chatSessionIds.map((sid) => {
                            const isActive = currentChatSessionId === sid
                            const rawTitle = String(sessionTitlesRef.current[sid] || '').trim()
                            const title = rawTitle && !isPlaceholderSessionTitle(rawTitle) ? rawTitle : '新对话'
                            return (
                              <div
                                key={sid}
                                className={`doc-studio__chat-header-tab doc-studio__chat-header-tab--with-close ${isActive ? 'doc-studio__chat-header-tab--active' : ''}`}
                                role="tab"
                                aria-selected={isActive}
                                onContextMenu={(event) => {
                                  event.preventDefault()
                                  event.stopPropagation()
                                  handleRenameChatSession(sid, title)
                                }}
                              >
                                <button
                                  type="button"
                                  className="doc-studio__chat-header-tab-label"
                                  onClick={() => handleSwitchChatSession(sid)}
                                  title={title}
                                >
                                  {title}
                                </button>
                                <Tooltip title="关闭（仍可从历史记录打开）">
                                  <button
                                    type="button"
                                    className="doc-studio__chat-header-tab-close"
                                    onClick={(e) => {
                                      e.stopPropagation()
                                      handleCloseChatSession(sid)
                                    }}
                                    aria-label="关闭对话"
                                  >
                                    <CloseOutlined />
                                  </button>
                                </Tooltip>
                              </div>
                            )
                          })}
                          {(!currentChatSessionId || hasNewConversationSlot) && (
                            <button
                              type="button"
                              className={`doc-studio__chat-header-tab ${!currentChatSessionId ? 'doc-studio__chat-header-tab--active' : ''}`}
                              onClick={async () => {
                                if (!snap.workspaceId) return
                                docStudioActions.setChatMessages([])
                                try {
                                  const detail = await bindWorkspaceSession(
                                    { workspaceId: snap.workspaceId, sessionId: null },
                                    { loading: false, errorToast: false },
                                  )
                                  docStudioActions.setWorkspaceConfig(detail.config)
                                } catch {
                                  docStudioActions.setWorkspaceConfig({
                                    ...snap.workspaceConfig,
                                    session_id: null,
                                  })
                                }
                              }}
                              title="新对话"
                            >
                              新对话
                            </button>
                          )}
                        </div>
                        <div className="doc-studio__chat-header-actions">
                          <Tooltip title="新建对话">
                            <button
                              type="button"
                              className="doc-studio__chat-header-icon"
                              onClick={handleNewChat}
                              disabled={!snap.workspaceId}
                            >
                              <PlusOutlined />
                            </button>
                          </Tooltip>
                          <Dropdown
                            trigger={['click']}
                            open={historyDropdownOpen}
                            onOpenChange={(nextOpen) => setHistoryDropdownOpen(nextOpen)}
                            overlayClassName="doc-studio__history-dropdown"
                            placement="bottomRight"
                            menu={{ items: [] }}
                            dropdownRender={() => {
                              const keyword = historySearchKeyword.trim().toLowerCase()
                              const openRows = chatSessionIds
                                .map((sid) => {
                                  const isActive = currentChatSessionId === sid
                                  const rawLabel = String(sessionTitlesRef.current[sid] || '').trim()
                                  const label =
                                    rawLabel && !isPlaceholderSessionTitle(rawLabel) ? rawLabel : '新对话'
                                  return { sid, label, isActive }
                                })
                                .filter((item) => !keyword || item.label.toLowerCase().includes(keyword))
                              const closedRows = closedSessionIds
                                .map((sid) => ({
                                  sid,
                                  label: (() => {
                                    const rawLabel = String(sessionTitlesRef.current[sid] || '').trim()
                                    return rawLabel && !isPlaceholderSessionTitle(rawLabel) ? rawLabel : '新对话'
                                  })(),
                                }))
                                .filter((item) => !keyword || item.label.toLowerCase().includes(keyword))

                              const hasRows = openRows.length + closedRows.length > 0
                              return (
                                <div
                                  className="doc-studio__history-dropdown-panel"
                                  onClick={(event) => event.stopPropagation()}
                                >
                                  <div className="doc-studio__history-dropdown-search">
                                    <Input
                                      allowClear
                                      size="small"
                                      value={historySearchKeyword}
                                      placeholder="Search..."
                                      prefix={<SearchOutlined />}
                                      onChange={(event) => setHistorySearchKeyword(event.target.value)}
                                      onKeyDown={(event) => event.stopPropagation()}
                                    />
                                  </div>
                                  {openRows.length > 0 && (
                                    <div className="doc-studio__history-dropdown-section">
                                      <div className="doc-studio__history-dropdown-section-title">Today</div>
                                      <div className="doc-studio__history-dropdown-list">
                                        {openRows.map((item) => (
                                          <button
                                            key={`history-open-${item.sid}`}
                                            type="button"
                                            className={`doc-studio__history-dropdown-row ${
                                              item.isActive ? 'doc-studio__history-dropdown-row--active' : ''
                                            }`}
                                            onClick={() => {
                                              void handleSwitchChatSession(item.sid).finally(() => {
                                                setHistoryDropdownOpen(false)
                                              })
                                            }}
                                          >
                                            <span className="doc-studio__history-dropdown-row-left">
                                              <span className="doc-studio__history-dropdown-row-check">
                                                {item.isActive ? <CheckOutlined /> : null}
                                              </span>
                                              <span
                                                className="doc-studio__history-dropdown-row-label"
                                                title={item.label}
                                              >
                                                {item.label}
                                              </span>
                                            </span>
                                            <span
                                              className="doc-studio__history-dropdown-row-actions"
                                              onClick={(event) => event.stopPropagation()}
                                            >
                                              <button
                                                type="button"
                                                className="doc-studio__history-dropdown-icon-btn"
                                                title="重命名对话"
                                                onClick={() => {
                                                  handleRenameChatSession(item.sid, item.label)
                                                }}
                                              >
                                                <EditOutlined />
                                              </button>
                                              <button
                                                type="button"
                                                className="doc-studio__history-dropdown-icon-btn doc-studio__history-dropdown-icon-btn--danger"
                                                title="删除对话"
                                                onClick={() => {
                                                  Modal.confirm({
                                                    title: '删除对话',
                                                    content: '确定彻底删除此对话？此操作不可恢复。',
                                                    okText: '删除',
                                                    okType: 'danger',
                                                    cancelText: '取消',
                                                    onOk: () => handleDeleteChatSession(item.sid),
                                                  })
                                                }}
                                              >
                                                <DeleteOutlined />
                                              </button>
                                            </span>
                                          </button>
                                        ))}
                                      </div>
                                    </div>
                                  )}
                                  {closedRows.length > 0 && (
                                    <div className="doc-studio__history-dropdown-section">
                                      <div className="doc-studio__history-dropdown-section-title">History</div>
                                      <div className="doc-studio__history-dropdown-list">
                                        {closedRows.map((item) => (
                                          <button
                                            key={`history-closed-${item.sid}`}
                                            type="button"
                                            className="doc-studio__history-dropdown-row doc-studio__history-dropdown-row--closed"
                                            onClick={() => {
                                              void handleReopenChatSession(item.sid).finally(() => {
                                                setHistoryDropdownOpen(false)
                                              })
                                            }}
                                          >
                                            <span className="doc-studio__history-dropdown-row-left">
                                              <span className="doc-studio__history-dropdown-row-check" />
                                              <span
                                                className="doc-studio__history-dropdown-row-label"
                                                title={item.label}
                                              >
                                                {item.label}
                                              </span>
                                            </span>
                                            <span
                                              className="doc-studio__history-dropdown-row-actions"
                                              onClick={(event) => event.stopPropagation()}
                                            >
                                              <button
                                                type="button"
                                                className="doc-studio__history-dropdown-icon-btn"
                                                title="重命名对话"
                                                onClick={() => {
                                                  handleRenameChatSession(item.sid, item.label)
                                                }}
                                              >
                                                <EditOutlined />
                                              </button>
                                              <button
                                                type="button"
                                                className="doc-studio__history-dropdown-icon-btn doc-studio__history-dropdown-icon-btn--danger"
                                                title="删除对话"
                                                onClick={() => {
                                                  Modal.confirm({
                                                    title: '删除对话',
                                                    content: '确定彻底删除此对话？此操作不可恢复。',
                                                    okText: '删除',
                                                    okType: 'danger',
                                                    cancelText: '取消',
                                                    onOk: () => handleDeleteChatSession(item.sid),
                                                  })
                                                }}
                                              >
                                                <DeleteOutlined />
                                              </button>
                                            </span>
                                          </button>
                                        ))}
                                      </div>
                                    </div>
                                  )}
                                  {!hasRows && (
                                    <div className="doc-studio__history-dropdown-empty">暂无历史对话</div>
                                  )}
                                </div>
                              )
                            }}
                          >
                            <button
                              type="button"
                              className="doc-studio__chat-header-icon"
                              aria-label="历史对话列表"
                            >
                              <HistoryOutlined />
                            </button>
                          </Dropdown>
                          {currentChatSessionId && (
                            <Dropdown
                              trigger={['click']}
                              menu={{
                                items: [
                                  {
                                    key: 'debug',
                                    icon: <BarChartOutlined />,
                                    label: '调试: 查看原始输出',
                                    onClick: async () => {
                                      if (!snap.workspaceId) return
                                      try {
                                        const data = await getWorkspaceMessagesDebug({
                                          workspaceId: snap.workspaceId,
                                          sessionId: currentChatSessionId,
                                        }, { loading: false, errorToast: false })
                                        setDebugData(data)
                                        setDebugModalOpen(true)
                                      } catch (e) {
                                        message.error('获取调试数据失败')
                                      }
                                    },
                                  },
                                  {
                                    key: 'delete',
                                    danger: true,
                                    icon: <DeleteOutlined />,
                                    label: '删除当前对话',
                                    onClick: () => {
                                      Modal.confirm({
                                        title: '删除对话',
                                        content: '确定删除当前对话？此操作不可恢复。',
                                        okText: '删除',
                                        okType: 'danger',
                                        cancelText: '取消',
                                        onOk: () => handleDeleteChatSession(currentChatSessionId),
                                      })
                                    },
                                  },
                                ],
                              }}
                            >
                              <button type="button" className="doc-studio__chat-header-icon">
                                <EllipsisOutlined />
                              </button>
                            </Dropdown>
                          )}
                          <Tooltip title="关闭右侧栏 (Ctrl+L 可重新打开)">
                            <button
                              type="button"
                              className="doc-studio__chat-header-icon"
                              onClick={() => setRightPanelClosed(true)}
                            >
                              <CloseOutlined />
                            </button>
                          </Tooltip>
                        </div>
                      </div>
                      {agentWarnings.length > 0 && (
                        <div className="doc-studio__chat-warnings">
                      {agentWarnings.map((warning, index) => (
                        <Alert
                          key={`agent-warning-${index}`}
                          type="warning"
                          showIcon
                          message="Agent 警告"
                          description={warning}
                          style={{ marginBottom: 8 }}
                        />
                                ))}
                              </div>
                            )}
                      <div ref={chatMessagesContainerRef} className="doc-studio__chat-messages">
                        {snap.chatMessages.length ? (
                          <>
                            {snap.chatMessages.map((msg, msgIndex) => {
                              const isReEditing = msg.role === 'user' && reEditDraft?.messageId === msg.id
                              const messageImages = Array.isArray(msg.meta?.images) ? msg.meta.images : []
                              const messageSelections = normalizeSelectionFragments(msg.meta?.selections)
                              const messageFileMentions = normalizeFileMentionFragments(
                                msg.meta?.fileMentions ?? msg.meta?.file_mentions,
                              )
                              const editingImages = isReEditing ? (reEditDraft?.images || []) : []
                              const displayImages = isReEditing ? editingImages : messageImages
                              const hasImages = displayImages.length > 0
                              let displayContent = msg.role === 'user' && hasImages
                                ? (msg.content || '').replace(/\n*\[已附带图片\s*\d+\s*张\]\s*/g, '').trim()
                                : msg.content
                              // 归一化：将 3 个及以上连续换行压缩为 2 个，避免产生空段落和过大间距
                              if (msg.role === 'agent' && typeof displayContent === 'string') {
                                displayContent = displayContent.replace(/\n{3,}/g, '\n\n')
                              }
                              const renderedUserContent = renderPromptWithMentionTags(
                                String(displayContent || ''),
                                messageSelections,
                                messageFileMentions,
                                handleMentionTagClick,
                              )
                              return (
                            <div
                              key={msg.id}
                              className={`doc-studio__chat-message doc-studio__chat-message--${msg.role}`}
                            >
                              {msg.role === 'user' ? (
                                <div className="doc-studio__chat-message-user-inner">
                                  <div className="doc-studio__chat-message-user-body">
                                    <div className="doc-studio__chat-message-user-content">
                                      {hasImages && !isReEditing && (
                                        <div className="doc-studio__chat-thumbnails">
                                          <Image.PreviewGroup>
                                            {displayImages.map((item: any, index: number) => {
                                              const url =
                                                typeof item?.dataUrl === 'string'
                                                  ? item.dataUrl
                                                  : typeof item?.data_url === 'string'
                                                    ? item.data_url
                                                    : ''
                                    if (!url) return null
                                              const name =
                                                typeof item?.name === 'string'
                                                  ? item.name
                                                  : `image-${index + 1}`
                                    return (
                                                <Image
                                        key={`${msg.id}-img-${index}`}
                                                  src={url}
                                                  alt={name}
                                                  width={36}
                                                  height={36}
                                                  rootClassName="doc-studio__chat-thumbnail-wrap"
                                                  preview={{ mask: '预览' }}
                                                />
                                              )
                                            })}
                                          </Image.PreviewGroup>
                                </div>
                              )}
                                      {isReEditing ? (
                                        <div
                                          className="doc-studio__chat-message-editor-wrap"
                                          ref={isReEditing ? reEditContainerRef : undefined}
                                        >
                                          {editingImages.length > 0 && (
                                            <div className="doc-studio__image-attachments doc-studio__chat-message-editor-images">
                                              <Space wrap size={[8, 8]}>
                                                {editingImages.map((item) => (
                                                  <span key={item.id} className="doc-studio__image-chip">
                                                    <Image
                                                      src={item.dataUrl}
                                                      alt={item.name}
                                                      width={36}
                                                      height={36}
                                                      className="doc-studio__image-chip-thumb"
                                                      preview={{ mask: false }}
                                                    />
                                                    <button
                                                      type="button"
                                                      className="doc-studio__image-chip-remove"
                                                      onClick={(e) => {
                                                        e.stopPropagation()
                                                        e.preventDefault()
                                                        removeReEditImageAttachment(item.id)
                                                      }}
                                                      title="移除图片"
                                                    >
                                                      ×
                                                    </button>
                                                  </span>
                                                ))}
                                              </Space>
                                            </div>
                                          )}
                                          <Input.TextArea
                                            autoSize={{ minRows: 2, maxRows: 8 }}
                                            className="doc-studio__chat-message-editor"
                                            placeholder="可编辑提示词，Ctrl+V 可粘贴图片（最多 4 张）"
                                            value={reEditDraft?.prompt || ''}
                                            onChange={(event) => {
                                              const nextPrompt = event.target.value
                                              setReEditDraft((prev) => {
                                                if (!prev || prev.messageId !== msg.id) return prev
                                                return { ...prev, prompt: nextPrompt }
                                              })
                                            }}
                                            onPaste={handleReEditPromptPaste}
                                            disabled={reEditSubmitting}
                                          />
                                          <div className="doc-studio__chat-message-editor-actions">
                                            <Button size="small" onClick={handleCancelReEdit} disabled={reEditSubmitting}>
                                              取消
                                            </Button>
                                            <Tooltip
                                              title="清理该消息后的对话，文件保持当前状态不回退"
                                              mouseEnterDelay={0.45}
                                            >
                                              <span>
                                                <Button
                                                  size="small"
                                                  onClick={() => {
                                                    void handleSubmitReEdit(false)
                                                  }}
                                                  disabled={reEditSubmitting}
                                                  loading={reEditSubmitting}
                                                >
                                                  继续但不恢复
                                                </Button>
                                              </span>
                                            </Tooltip>
                                            <Tooltip
                                              title={
                                                reEditDraft?.runId
                                                  ? '清理后续对话，并将文件回退到该消息发送前的 checkpoint'
                                                  : '当前消息无 checkpoint，将自动降级为仅清理后续对话'
                                              }
                                              mouseEnterDelay={0.45}
                                            >
                                              <span>
                                                <Button
                                                  size="small"
                                                  type="primary"
                                                  onClick={() => {
                                                    void handleSubmitReEdit(true)
                                                  }}
                                                  disabled={reEditSubmitting}
                                                  loading={reEditSubmitting}
                                                >
                                                  继续并恢复
                                                </Button>
                                              </span>
                                            </Tooltip>
                                          </div>
                                        </div>
                                      ) : (
                                        <div
                                          className="doc-studio__chat-content doc-studio__chat-content--editable"
                                          role="button"
                                          tabIndex={0}
                                          onClick={() => handleReEditMessage(msg, msgIndex)}
                                          onKeyDown={(event) => {
                                            if (event.key === 'Enter' || event.key === ' ') {
                                              event.preventDefault()
                                              handleReEditMessage(msg, msgIndex)
                                            }
                                          }}
                                        >
                                          {renderedUserContent}
                                        </div>
                                      )}
                                    </div>
                                    {!isReEditing && (
                                      <Tooltip title="直接编辑并继续">
                                        <button
                                          type="button"
                                          className="doc-studio__chat-message-reset"
                                          onClick={() => handleReEditMessage(msg, msgIndex)}
                                          aria-label="重新编辑"
                                        >
                                          <RollbackOutlined />
                                        </button>
                                      </Tooltip>
                                    )}
                                  </div>
                                </div>
                              ) : (
                                <>
                                  <div className="doc-studio__chat-content doc-studio__chat-content--markdown">
                                    <ChatMarkdown onSceneRefClick={handleSceneRefJump}>
                                      {displayContent}
                                    </ChatMarkdown>
                                  </div>
                                  <div className="doc-studio__chat-feedback">
                                    <Tooltip title="复制回答">
                                      <Button
                                        size="small"
                                        icon={<CopyOutlined />}
                                        onClick={() => {
                                          const text = String(displayContent || '')
                                          if (!text.trim()) {
                                            message.warning('暂无可复制内容')
                                            return
                                          }
                                          copyTextToClipboard(text)
                                            .then(() => message.success('回答已复制'))
                                            .catch(() => message.error('复制失败，请手动复制'))
                                        }}
                                      />
                                    </Tooltip>
                                    <Dropdown
                                      menu={{
                                        items: [
                                          {
                                            key: 'export-txt',
                                            label: '导出为 TXT',
                                            onClick: () => {
                                              const text = String(displayContent || '')
                                              if (!text.trim()) {
                                                message.warning('暂无可导出内容')
                                                return
                                              }
                                              downloadTextAsFile(
                                                text,
                                                `doc-studio-reply-${Date.now()}.txt`,
                                              )
                                            },
                                          },
                                        ],
                                      }}
                                      trigger={['click']}
                                    >
                                      <Button size="small" icon={<ShareAltOutlined />} />
                                    </Dropdown>
                                    {msg.meta?.traceId && (
                                      <>
                                        <Tooltip title="有帮助">
                                          <Button
                                            size="small"
                                            type={msg.meta?.feedback === 'thumbs_up' ? 'primary' : 'default'}
                                            icon={<LikeOutlined />}
                                            onClick={() =>
                                              handleFeedbackSubmit(msg.id, msg.meta?.traceId, 'thumbs_up')
                                            }
                                            loading={!!feedbackSubmitting[msg.id]}
                                          />
                                        </Tooltip>
                                        <Tooltip title="无帮助">
                                          <Button
                                            size="small"
                                            type={msg.meta?.feedback === 'thumbs_down' ? 'primary' : 'default'}
                                            icon={<DislikeOutlined />}
                                            onClick={() =>
                                              handleFeedbackSubmit(msg.id, msg.meta?.traceId, 'thumbs_down')
                                            }
                                            loading={!!feedbackSubmitting[msg.id]}
                                          />
                                        </Tooltip>
                                      </>
                                    )}
                                  </div>
                                </>
                              )}
                            </div>
                            )
                            })}
                            {chatLoading && (
                              <div className="doc-studio__chat-live">
                                <div className="doc-studio__chat-live-head">
                                  <Spin size="small" />
                                  <span>{liveAgentStatus || '任务执行中...'}</span>
                                  <span className="doc-studio__chat-live-time">{liveAgentElapsedSec}s</span>
                                  {liveDeltaCharCount > 0 && (
                                    <span className="doc-studio__chat-live-counter">
                                      {liveDeltaCharCount} chars
                                    </span>
                                  )}
                                </div>
                                {liveDeltaStartedRef.current && (
                                  <div ref={liveOutputRef} className="doc-studio__chat-live-output">
                                    {livePreviewLines.map((line, lineIndex) => (
                                      <div
                                        key={`live-preview-${lineIndex}`}
                                        className="doc-studio__chat-live-output-line"
                                      >
                                        {line}
                              </div>
                            ))}
                                  </div>
                                )}
                              </div>
                            )}
                            <div ref={chatMessagesEndRef} />
                          </>
                        ) : chatLoading ? (
                          <>
                            <div className="doc-studio__chat-live">
                              <div className="doc-studio__chat-live-head">
                                <Spin size="small" />
                                <span>{liveAgentStatus || '任务执行中...'}</span>
                                <span className="doc-studio__chat-live-time">{liveAgentElapsedSec}s</span>
                                {liveDeltaCharCount > 0 && (
                                  <span className="doc-studio__chat-live-counter">
                                    {liveDeltaCharCount} chars
                                  </span>
                                )}
                              </div>
                              {liveDeltaStartedRef.current && (
                                <div ref={liveOutputRef} className="doc-studio__chat-live-output">
                                  {livePreviewLines.map((line, lineIndex) => (
                                    <div
                                      key={`live-preview-empty-${lineIndex}`}
                                      className="doc-studio__chat-live-output-line"
                                    >
                                      {line}
                                    </div>
                                  ))}
                                </div>
                              )}
                            </div>
                            <div ref={chatMessagesEndRef} />
                          </>
                        ) : (
                          <div className="doc-studio__chat-empty">
                            <Empty
                              description="暂无对话"
                              image={Empty.PRESENTED_IMAGE_SIMPLE}
                            />
                          </div>
                        )}
                      </div>
                      {/* ScriptLens M3：场景改写入口（仅当激活了 scene 时显示） */}
                      {snap.activeFilePath && snap.workspaceId ? (
                        <div className="doc-studio__chat-review-actions">
                          <Tooltip title="基于评分维度让 LLM 改写当前场景，diff 在中央 pane 比对">
                            <Button
                              size="small"
                              icon={<EditOutlined />}
                              onClick={() => {
                                setRewriteIssue('')
                                setRewriteModalOpen(true)
                              }}
                            >
                              AI 改写本场
                            </Button>
                          </Tooltip>
                        </div>
                      ) : null}
                      {hasPendingAgentReview && (
                        <div className="doc-studio__chat-review-actions">
                          <Button
                            type="text"
                            danger
                            size="small"
                            disabled={diffReverting}
                            onClick={() => {
                              void handleRejectAllDiffs()
                            }}
                          >
                            Undo All
                          </Button>
                          <Button
                            type="text"
                            size="small"
                            onClick={handleKeepAllDiffs}
                          >
                            Keep All
                          </Button>
                          <Button
                            size="small"
                            type={isAgentDiffReviewActive ? 'default' : 'primary'}
                            onClick={() => {
                              setAgentDiffReviewOpen(true)
                              setDiffModalContext('agent')
                            }}
                          >
                            Review
                          </Button>
                        </div>
                      )}
                      <div ref={chatInputContainerRef} className="doc-studio__chat-input">
                        {chatImageAttachments.length > 0 && (
                          <div className="doc-studio__image-attachments">
                            <Space wrap size={[8, 8]}>
                              {chatImageAttachments.map((item) => (
                                <span key={item.id} className="doc-studio__image-chip">
                                  <Image
                                    src={item.dataUrl}
                                    alt={item.name}
                                    width={36}
                                    height={36}
                                    className="doc-studio__image-chip-thumb"
                                    preview={{ mask: false }}
                                  />
                                  <button
                                    type="button"
                                    className="doc-studio__image-chip-remove"
                                    onClick={(e) => {
                                      e.stopPropagation()
                                      e.preventDefault()
                                      removeChatImageAttachment(item.id)
                                    }}
                                    title="移除图片"
                                  >
                                    ×
                                  </button>
                                </span>
                              ))}
                            </Space>
                          </div>
                        )}
                        <div className="doc-studio__prompt-wrapper" ref={promptWrapperRef}>
                          <div
                            ref={(el) => {
                              if (el) {
                                promptInputDivRef.current = el
                                // @ts-ignore
                                if (promptInputRef.current !== el) {
                                  // @ts-ignore
                                  promptInputRef.current = { resizableTextArea: { textArea: el } }
                                }
                              }
                            }}
                            className="doc-studio__prompt-input"
                            contentEditable
                            suppressContentEditableWarning
                            data-placeholder={
                              selections.length || fileMentions.length
                                ? `已选 ${selections.length} 段，已引 ${fileMentions.length} 个文件，输入 @ 可引用文件`
                                : '输入指令，Ctrl+V 粘贴图片，Ctrl+L 引用选区，Enter 发送，Shift+Enter 换行'
                            }
                            onInput={(e) => {
                              const target = e.currentTarget
                              const text = extractTextFromDiv(target)
                              setPrompt(text)
                            }}
                            onClick={(e) => {
                              const target = e.target as HTMLElement
                              const closeNode = target.closest('.prompt-tag-close') as HTMLElement | null
                              if (closeNode) {
                                const placeholder = closeNode.getAttribute('data-action')?.replace('remove-', '')
                                if (placeholder) {
                                  removeComposerMentionToken(placeholder)
                                }
                              }
                            }}
                            onPaste={handlePromptPaste}
                            onKeyDown={(event) => {
                              const lowerKey = event.key.toLowerCase()
                              if (fileMentionDropdownOpen) {
                                if (lowerKey === 'arrowdown') {
                                  event.preventDefault()
                                  setFileMentionActiveIndex((prev) =>
                                    Math.min(prev + 1, fileMentionCandidates.length - 1),
                                  )
                                  return
                                }
                                if (lowerKey === 'arrowup') {
                                  event.preventDefault()
                                  setFileMentionActiveIndex((prev) => Math.max(prev - 1, 0))
                                  return
                                }
                                if (lowerKey === 'escape') {
                                  event.preventDefault()
                                  clearFileMentionSuggest()
                                  return
                                }
                                if (lowerKey === 'enter') {
                                  event.preventDefault()
                                  const targetPath = fileMentionCandidates[fileMentionActiveIndex]
                                  if (targetPath) {
                                    addFileMentionFromCandidate(targetPath)
                                  }
                                  return
                                }
                              }
                              if (lowerKey === 'enter') {
                                if (event.shiftKey) {
                                  // Shift+Enter：换行
                                  return
                                }
                                // Enter / Ctrl+Enter：发送
                                event.preventDefault()
                                if (chatLoading) {
                                  void handleStopSending()
                                } else {
                                  handleSend()
                                }
                                return
                              }
                              if ((event.ctrlKey || event.metaKey) && lowerKey === 'l') {
                                event.preventDefault()
                                addSelectionSnippet()
                              }
                            }}
                          />
                          {fileMentionDropdownOpen && (
                            <div className="doc-studio__mention-dropdown" role="listbox" aria-label="文件引用候选列表">
                              <div className="doc-studio__mention-dropdown-header">
                                <span>Files &amp; Folders</span>
                                <span className="doc-studio__mention-dropdown-hint">Enter 选择 · Esc 关闭</span>
                              </div>
                              {fileMentionCandidates.map((item, index) => (
                                <button
                                  key={item}
                                  type="button"
                                  className={`doc-studio__mention-dropdown-item ${
                                    index === fileMentionActiveIndex
                                      ? 'doc-studio__mention-dropdown-item--active'
                                      : ''
                                  }`}
                                  onMouseDown={(event) => {
                                    event.preventDefault()
                                    addFileMentionFromCandidate(item)
                                  }}
                                >
                                  <span className="doc-studio__mention-dropdown-item-icon" aria-hidden>
                                    <FileTextOutlined />
                                  </span>
                                  <span className="doc-studio__mention-dropdown-item-main">
                                    <span className="doc-studio__mention-dropdown-item-name">
                                      {item.split('/').pop()}
                                    </span>
                                    <span className="doc-studio__mention-dropdown-item-path">
                                      {item}
                                    </span>
                                  </span>
                                </button>
                              ))}
                            </div>
                          )}
                        </div>
                        <div className="doc-studio__chat-toolbar">
                          <Select
                            size="small"
                            className="doc-studio__chat-mode-select"
                            popupMatchSelectWidth={false}
                            style={{ width: modeSelectWidth }}
                            value={interactionMode}
                            options={[
                              { label: 'Ask', value: 'ask' },
                              { label: 'Agent', value: 'agent' },
                            ]}
                            onChange={(value) => setInteractionMode(value as InteractionMode)}
                          />
                          <Select
                            size="small"
                            className="doc-studio__chat-model-select"
                            popupMatchSelectWidth={false}
                            style={{ width: modelSelectWidth }}
                            value={llmModel}
                            loading={llmModelCatalogLoading}
                            options={llmModelOptions.map((item) => ({
                              label: item.available === false ? `${item.label}（不可用）` : item.label,
                              value: item.value,
                              disabled: item.available === false,
                            }))}
                            onChange={(value) => setLlmModel(normalizeRuntimeLlmModel(value))}
                          />
                          {!chatToolbarCompact && ragEnabled && (
                            <Select
                              size="small"
                              value={selectedKnowledgeBaseId ?? undefined}
                              className="doc-studio__chat-rag-select"
                              popupMatchSelectWidth={false}
                              style={{ width: ragSelectWidth }}
                              placeholder="知识库"
                              options={knowledgeBaseOptions}
                              loading={knowledgeLoading}
                              onChange={handleKnowledgeBaseChange}
                              disabled={knowledgeLoading}
                              allowClear
                              showSearch
                              optionFilterProp="label"
                              notFoundContent={
                                knowledgeLoading ? (
                                  <Spin size="small" />
                                ) : (
                                  <span>暂无知识库</span>
                                )
                              }
                            />
                          )}
                          {chatToolbarCompact && (
                            <Dropdown
                              trigger={['click']}
                              menu={{
                                items: [
                                  {
                                    key: 'rag-toggle',
                                    label: ragEnabled ? '关闭 RAG 检索' : '开启 RAG 检索',
                                    icon: <DatabaseOutlined />,
                                    onClick: handleToggleRagEnabled,
                                  },
                                  { key: 'web', label: 'Web 搜索', icon: <GlobalOutlined />, disabled: true },
                                  {
                                    key: 'image',
                                    label: '添加图片',
                                    icon: <PictureOutlined />,
                                    onClick: () => chatImageInputRef.current?.click(),
                                  },
                                  {
                                    key: 'status',
                                    label: '系统状态',
                                    icon: <BarChartOutlined />,
                                    onClick: () => setSystemStatusOpen(true),
                                  },
                                ],
                              }}
                            >
                              <button type="button" className="doc-studio__chat-toolbar-more-btn" title="更多选项">
                                <EllipsisOutlined />
                              </button>
                            </Dropdown>
                          )}
                          <div className="doc-studio__chat-toolbar-actions">
                            {!chatToolbarCompact && (
                            <div className="doc-studio__chat-icon-cluster">
                              <Tooltip title={ragEnabled ? '关闭 RAG 检索' : '开启 RAG 检索'}>
                                <Button
                                  type="text"
                                  className={`doc-studio__toolbar-icon-btn ${
                                    ragEnabled ? 'doc-studio__toolbar-icon-btn--active' : ''
                                  }`}
                                  icon={<DatabaseOutlined />}
                                  onClick={handleToggleRagEnabled}
                                />
                              </Tooltip>
                              <Tooltip title="Web 搜索默认开启">
                                <Button
                                  type="text"
                                  className="doc-studio__toolbar-icon-btn doc-studio__toolbar-icon-btn--active"
                                  icon={<GlobalOutlined />}
                                />
                              </Tooltip>
                              <Tooltip title="添加图片">
                                <Button
                                  type="text"
                                  className="doc-studio__toolbar-icon-btn"
                                  icon={<PictureOutlined />}
                                  onClick={handleChatImagePickerClick}
                                  loading={chatImageProcessing}
                                  disabled={!snap.workspaceId}
                                />
                              </Tooltip>
                              <Tooltip title="系统状态">
                                <Button
                                  type="text"
                                  className="doc-studio__toolbar-icon-btn"
                                  icon={<BarChartOutlined />}
                                  onClick={() => setSystemStatusOpen(true)}
                                />
                              </Tooltip>
                            </div>
                            )}
                            <Tooltip title="语音输入">
                              <Recorder
                                buttonClassName="doc-studio__voice-btn"
                                activeButtonClassName="doc-studio__voice-btn--recording"
                                disabled={!snap.workspaceId}
                                onMessage={(text) => {
                                  setPrompt(String(text || ''))
                                  setTimeout(() => {
                                    promptInputDivRef.current?.focus()
                                  }, 0)
                                }}
                              />
                            </Tooltip>
                            <Button
                              type="primary"
                              icon={
                                chatLoading ? (
                                  <span className="doc-studio__send-stop-icon" />
                                ) : (
                                  <ArrowUpOutlined />
                                )
                              }
                              className="doc-studio__send-btn"
                              title={chatLoading ? '中断' : '发送（Ctrl+Enter）'}
                              onClick={chatLoading ? handleStopSending : () => void handleSend()}
                              disabled={
                                !snap.workspaceId ||
                                (!chatLoading && !prompt.trim() && chatImageAttachments.length === 0)
                              }
                            />
                          </div>
                        </div>
                        <input
                          ref={chatImageInputRef}
                          type="file"
                          accept="image/*"
                          multiple
                          style={{ display: 'none' }}
                          onChange={handleChatImageInputChange}
                        />
                      </div>
                    </div>
                )}
                {/* History Panel */}
                {rightTab === 'history' && (
                <div className="doc-studio__history">
                  <div className="doc-studio__timeline-toolbar">
                    <Text strong>文件时间线</Text>
                    <Space size={8} wrap>
                      {snap.activeFilePath && (
                        <Text code className="doc-studio__timeline-file-tag">
                          {snap.activeFilePath}
                        </Text>
                      )}
                      <Button
                        size="small"
                        icon={<ReloadOutlined />}
                        loading={operationHistoryLoading}
                        onClick={() => {
                          void loadOperationHistory()
                        }}
                        disabled={!snap.workspaceId}
                      >
                        刷新
                      </Button>
                    </Space>
                  </div>
                  {!snap.activeFilePath ? (
                    <Empty description="请先在左侧选择文件" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                  ) : operationHistoryLoading && !activeFileTimeline.length ? (
                    <div style={{ padding: '18px 0', textAlign: 'center' }}>
                      <Spin size="small" />
                    </div>
                  ) : activeFileTimeline.length ? (
                    <Timeline
                      className="doc-studio__history-timeline"
                      mode="left"
                      items={activeFileTimeline.map((item) => {
                        const modifiedCount = item.modified_files?.length || 0
                        const timestamp = item.timestamp
                          ? new Date(item.timestamp).toLocaleString()
                          : '未知时间'
                        return {
                          color: item.success ? 'blue' : 'red',
                          children: (
                            <div className="doc-studio__timeline-card">
                              <div className="doc-studio__timeline-head">
                                <Text strong>{item.user_intent || '未命名操作'}</Text>
                                <Tag color={item.success ? 'green' : 'red'}>
                                  {item.success ? '成功' : '失败'}
                                </Tag>
                                {item.intent_type ? <Tag>{item.intent_type}</Tag> : null}
                              </div>
                              <Space size="small" wrap>
                                <Text type="secondary">{timestamp}</Text>
                                <Text type="secondary">改动文件: {modifiedCount}</Text>
                                <Text type="secondary">Op: {item.operation_id.slice(0, 8)}</Text>
                              </Space>
                              <div className="doc-studio__timeline-actions">
                                <Button
                                  size="small"
                                  icon={<EyeOutlined />}
                                  onClick={() =>
                                    openTimelineDiffPreview(item.operation_id, snap.activeFilePath)
                                  }
                                >
                                  预览差异
                                </Button>
                                <Tooltip title="ScriptLens 当前不持久化改写到原文，因此回退仅做占位（点击后会提示无可恢复内容）。原始文档始终是上传时的版本。">
                                  <Popconfirm
                                    title="恢复当前文件到该时间点？"
                                    onConfirm={() =>
                                      handleRevertOperation(
                                        item.operation_id,
                                        snap.activeFilePath
                                          ? [normalizeWorkspacePath(snap.activeFilePath)]
                                          : undefined,
                                      )
                                    }
                                  >
                                    <Button
                                      size="small"
                                      loading={revertingOperationId === item.operation_id}
                                    >
                                      恢复此版本
                                    </Button>
                                  </Popconfirm>
                                </Tooltip>
                              </div>
                            </div>
                          ),
                        }
                      })}
                    />
                  ) : (
                    <Empty
                      description="当前文件暂无时间线记录"
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                    />
                  )}
                </div>
                )}
                {/* Compile Panel */}
                {supportsCompilePanel && rightTab === 'compile' && (
                  <div className="doc-studio__compile">
                      {snap.compileResult ? (
                        compileFormat === 'markdown' ? (
                          markdownCompilePreviewContent ? (
                            <div className="doc-studio__compile-markdown-preview doc-studio__compile-markdown-preview--full">
                              <ChatMarkdown>{markdownCompilePreviewContent}</ChatMarkdown>
                            </div>
                          ) : (
                            <Empty
                              description="暂无可渲染的 Markdown 内容"
                              image={Empty.PRESENTED_IMAGE_SIMPLE}
                            />
                          )
                        ) : (
                          <>
                            <Text type={snap.compileResult.success ? 'success' : 'danger'}>
                              {snap.compileResult.summary || (snap.compileResult.success ? '编译成功' : '编译失败')}
                            </Text>
                            {!snap.compileResult.success && snap.compileResult.error ? (
                              <Alert
                                type="error"
                                showIcon
                                message="编译错误"
                                description={snap.compileResult.error}
                              />
                            ) : null}
                            <div className="doc-studio__compile-actions">
                              <Button
                                type="primary"
                                icon={<EyeOutlined />}
                                size="small"
                                onClick={handlePreviewPdf}
                                disabled={!snap.compileResult.data?.pdf_path}
                              >
                                预览 PDF
                              </Button>
                              <Button
                                icon={<DownloadOutlined />}
                                size="small"
                                onClick={handleDownloadPdf}
                                disabled={!snap.compileResult.data?.pdf_path}
                              >
                                下载 PDF
                              </Button>
                              <Button
                                icon={<SyncOutlined />}
                                size="small"
                                onClick={async () => {
                                  if (!snap.workspaceId) return
                                  const status = await fetchCompileStatus({ workspaceId: snap.workspaceId })
                                  if (status?.result) {
                                    docStudioActions.setCompileResult({
                                      success: status.result.success,
                                      data: status.result.data,
                                      error: status.result.error ?? undefined,
                                      summary: status.result.summary ?? undefined,
                                    })
                                    setRightTab('compile')
                                  } else {
                                    message.info('暂无编译结果')
                                  }
                                }}
                              >
                                刷新状态
                              </Button>
                            </div>
                            {snap.compileResult.data?.pdf_path && (
                              <div>
                                <Text type="secondary">PDF 路径</Text>
                                <Text code>{snap.compileResult.data.pdf_path}</Text>
                              </div>
                            )}
                            {snap.compileResult.data?.warnings?.length ? (
                              <div className="doc-studio__compile-section">
                                <Text type="warning">警告</Text>
                                <ul>
                                  {snap.compileResult.data.warnings.map((warning, idx) => (
                                    <li key={`warning-${idx}`}>{warning}</li>
                                  ))}
                                </ul>
                              </div>
                            ) : null}
                            {snap.compileResult.data?.errors?.length ? (
                              <div className="doc-studio__compile-section">
                                <Text type="danger">错误</Text>
                                <ul>
                                  {snap.compileResult.data.errors.map((errorMsg, idx) => (
                                    <li key={`error-${idx}`}>{errorMsg}</li>
                                  ))}
                                </ul>
                              </div>
                            ) : null}
                            {snap.compileResult.data?.logs?.length ? (
                              <div className="doc-studio__compile-section doc-studio__compile-logs">
                                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                                  <Text strong style={{ fontSize: 14 }}>编译日志</Text>
                                  <Button
                                    size="small"
                                    onClick={async () => {
                                      const allLogs =
                                        compileLogGroups
                                          ?.map((log) =>
                                            `=== ${log.command} (返回码: ${log.returncode}${
                                              log.count > 1 ? `, 重复 ${log.count} 次` : ''
                                            }) ===\n${log.log || '(无日志)'}`
                                          )
                                          .join('\n\n') || ''
                                      if (!allLogs) {
                                        message.info('没有可复制的日志')
                                        return
                                      }
                                      try {
                                        await copyTextToClipboard(allLogs)
                                        message.success('日志已复制')
                                      } catch (error) {
                                        // ??????????Clipboard API
                                        // ????????copyTextToClipboard ????????????
                                        message.error('复制失败，请手动选择')
                                      }
                                    }}
                                  >
                                    复制日志
                                  </Button>
                                </div>
                                {compileLogGroups.map((log, idx) => {
                                  const logLines = (log.log || '').split('\n')
                                  // ????????????????
                                  const commandName = log.command.split(' ')[0] || 'unknown'
                                  const stepName =
                                    log.firstIndex === 0
                                      ? '编译引擎'
                                      : commandName.includes('bibtex')
                                        ? 'BibTeX 处理'
                                        : '后续编译'
                                  return (
                                    <div key={`log-${idx}`} className="doc-studio__compile-log-block">
                                      <div className="doc-studio__compile-log-header">
                                        <Tag color={log.returncode === 0 ? 'green' : 'red'}>
                                          返回码 {log.returncode}
                                        </Tag>
                                        <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                                          {stepName}
                                        </Text>
                                        {log.count > 1 && (
                                          <Tag color="blue" style={{ marginLeft: 8 }}>
                                            重复 x{log.count}
                                          </Tag>
                                        )}
                                        <Text type="secondary" code style={{ flex: 1, marginLeft: 8, fontSize: 11 }}>
                                          {log.command}
                                        </Text>
                                      </div>
                                      <div className="doc-studio__compile-log">
                                        {logLines.length > 0 ? (
                                          logLines.map((line, lineIdx) => {
                                            const trimmedLine = line.trim()
                                            const isError = trimmedLine.startsWith('!') ||
                                                           trimmedLine.includes('Error') ||
                                                           trimmedLine.includes('Fatal error') ||
                                                           trimmedLine.includes('Missing character')
                                            const isWarning = trimmedLine.includes('Warning') ||
                                                             trimmedLine.includes('LaTeX Warning')
                                            const isInfo = trimmedLine.includes('Output written') ||
                                                          trimmedLine.includes('Transcript written') ||
                                                          trimmedLine.includes('This is')

                                            let className = ''
                                            if (isError) className = 'doc-studio__compile-log-line--error'
                                            else if (isWarning) className = 'doc-studio__compile-log-line--warning'
                                            else if (isInfo) className = 'doc-studio__compile-log-line--info'

                                            return (
                                              <div
                                                key={`line-${lineIdx}`}
                                                className={className}
                                                style={{
                                                  padding: '2px 0',
                                                  fontFamily: 'SFMono-Regular, Consolas, Liberation Mono, Menlo, monospace',
                                                  fontSize: '12px',
                                                  lineHeight: '1.5'
                                                }}
                                              >
                                                {line || '\u00A0'}
                                              </div>
                                            )
                                          })
                                        ) : (
                                          <div style={{ color: '#888', fontStyle: 'italic' }}>(无日志)</div>
                                        )}
                                      </div>
                                    </div>
                                  )
                                })}
                              </div>
                            ) : null}
                          </>
                        )
                      ) : (
                        <Empty
                          description="暂无编译结果"
                          image={Empty.PRESENTED_IMAGE_SIMPLE}
                        />
                      )}
                    </div>
                )}
              </div>
              </div>
            </div>
          </Sider>
            </>
          )}
          {(isDraggingLeft || isDraggingRight) && <div className="doc-studio__drag-mask" />}
        </Layout>
      </div>
      <Modal
        title="上传剧本"
        open={workspaceModalOpen}
        onOk={handleCreateWorkspace}
        okText={newWorkspaceFile ? `上传：${newWorkspaceFile.name}` : '请选择文件'}
        onCancel={() => {
          setWorkspaceModalOpen(false)
          setNewWorkspaceFile(null)
          setNewWorkspaceType('latex')
        }}
        confirmLoading={workspaceSubmitting}
      >
        <Form layout="vertical">
          <Form.Item
            label="剧本文件"
            extra="支持 .docx / .pdf / .txt / .md，单文件 ≤50MB"
          >
            <input
              type="file"
              accept=".docx,.pdf,.txt,.md,.markdown"
              onChange={(event) => {
                const file = event.target.files?.[0] || null
                setNewWorkspaceFile(file)
                if (file && !newWorkspaceName.trim()) {
                  setNewWorkspaceName(file.name.replace(/\.[^.]+$/, ''))
                }
              }}
            />
          </Form.Item>
          <Form.Item label="剧本标题（可选，不填用文件名）">
            <Input
              placeholder="例如：闪婚总裁夜夜宠"
              value={newWorkspaceName}
              onChange={(event) => setNewWorkspaceName(event.target.value)}
            />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title="AI 改写本场（rubric §3 五维之一）"
        open={rewriteModalOpen}
        onOk={() => {
          void handleSubmitRewrite()
        }}
        okText="开始改写"
        onCancel={() => {
          if (rewriteSubmitting) return
          setRewriteModalOpen(false)
          setRewriteIssue('')
        }}
        confirmLoading={rewriteSubmitting}
        destroyOnClose
        maskClosable={!rewriteSubmitting}
        keyboard={!rewriteSubmitting}
        width={520}
      >
        <Form layout="vertical">
          <Form.Item label="目标场景">
            <Input value={activeSceneLabel} disabled />
          </Form.Item>
          <Form.Item
            label="聚焦改写维度"
            extra="后端会基于该维度的 rubric 锚点和原始场景文本生成改写"
          >
            <Select
              value={rewriteDimension}
              onChange={(v) => setRewriteDimension(v)}
              options={[
                { value: 'opening_hook', label: '开场钩子（前 3 集前 3 场抓人）' },
                { value: 'reward_density', label: '爽点密度（反转 / 打脸 / 逆袭）' },
                { value: 'motivation', label: '动机自洽（关键决策铺垫）' },
                { value: 'pacing', label: '节奏控制（中段不塌陷）' },
                { value: 'risk', label: '审核风险（去广电红线）' },
              ]}
            />
          </Form.Item>
          <Form.Item
            label="问题描述（必填）"
            required
            extra="尽量具体：例如『女主原谅得太快缺乏铺垫』"
          >
            <Input.TextArea
              value={rewriteIssue}
              onChange={(e) => setRewriteIssue(e.target.value)}
              placeholder="例如：钩子太弱，开篇 30 秒看不出爽点 / 男主黑化没有动机铺垫"
              maxLength={500}
              rows={3}
              showCount
            />
          </Form.Item>
          <Alert
            type="info"
            showIcon
            message="改写完成后会在中央 pane 切到 in-place diff（保留原文，可 Keep / Undo 单 hunk）。"
          />
        </Form>
      </Modal>
      <Modal
        title={fileModalType === 'file' ? '新建文件' : '新建文件夹'}
        open={fileModalOpen}
        onOk={handleCreateFile}
        onCancel={() => setFileModalOpen(false)}
        confirmLoading={fileSubmitting}
      >
        <Form layout="vertical">
          <Form.Item label={fileModalType === 'file' ? '文件路径（含文件名）' : '目录路径'}>
            <Input
              placeholder={fileModalType === 'file' ? 'sections/intro.tex' : 'sections'}
              value={fileModalPath}
              onChange={(event) => setFileModalPath(event.target.value)}
            />
          </Form.Item>
          {fileModalType === 'file' && (
            <Form.Item label="文件初始内容（可选）">
              <Input.TextArea
                rows={4}
                placeholder="可留空；这里填写文件正文，不是文件名"
                value={fileModalContent}
                onChange={(event) => setFileModalContent(event.target.value)}
              />
            </Form.Item>
          )}
        </Form>
      </Modal>
      <Modal
        title={renameSourceType === 'directory' ? '重命名文件夹' : '重命名文件'}
        open={renameModalOpen}
        onOk={handleRenamePath}
        onCancel={() => {
          setRenameModalOpen(false)
          setRenameNameInput('')
          setRenameSourcePath('')
        }}
        confirmLoading={renameSubmitting}
      >
        <Form layout="vertical">
          <Form.Item label="新名称">
            <Input
              autoFocus
              value={renameNameInput}
              placeholder={renameSourceType === 'directory' ? '例如: sections' : '例如: intro.tex'}
              onChange={(event) => setRenameNameInput(event.target.value)}
              onPressEnter={() => {
                void handleRenamePath()
              }}
            />
          </Form.Item>
        </Form>
      </Modal>
      {/* Agent ???? Modal */}
      <Modal
        title={`版本对比 - ${allFileDiffs[currentDiffIndex]?.file_path || ''}`}
        open={diffModalOpen && diffModalContext === 'timeline'}
        onCancel={() => {
          closeDiffModal(allFileDiffs.map((d) => d.file_path).filter((p): p is string => Boolean(p)))
        }}
        width="90%"
        style={{ top: 20 }}
        footer={
          <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
            <Button
              onClick={() =>
                closeDiffModal(allFileDiffs.map((d) => d.file_path).filter((p): p is string => Boolean(p)))
              }
            >
              关闭
            </Button>
            <Popconfirm
              title="恢复当前文件到该时间点？"
              onConfirm={() => {
                if (!diffOperationId) return
                return handleRevertOperation(
                  diffOperationId,
                  allFileDiffs[currentDiffIndex]?.file_path
                    ? [allFileDiffs[currentDiffIndex].file_path]
                    : undefined,
                )
              }}
            >
              <Button
                type="primary"
                loading={!!(diffOperationId && revertingOperationId === diffOperationId)}
                disabled={!diffOperationId || !allFileDiffs[currentDiffIndex]?.file_path}
              >
                恢复此版本
              </Button>
            </Popconfirm>
          </Space>
        }
      >
        <div className="doc-studio__diff-wrapper doc-studio__diff-wrapper--timeline">
          <div className="doc-studio__diff-view">
            {allFileDiffs.length > 0 && allFileDiffs[currentDiffIndex] && (
              <AgentDiffReview
                key={`timeline-${currentDiffIndex}-${allFileDiffs[currentDiffIndex]?.file_path}`}
                filePath={allFileDiffs[currentDiffIndex].file_path || ''}
                originalContent={resolvedOriginal || allFileDiffs[currentDiffIndex].original_content}
                modifiedContent={resolvedModified || allFileDiffs[currentDiffIndex].modified_content}
                readOnly
              />
            )}
            {allFileDiffs[currentDiffIndex]?.is_truncated && (
              <Alert
                style={{ marginTop: 12 }}
                type="info"
                showIcon
                message="内容已截断"
                description="由于内容过长已截断显示，如需完整内容请下载文件查看。"
              />
            )}
          </div>
        </div>
      </Modal>

      <Modal
        title="系统状态"
        open={systemStatusOpen}
        onCancel={() => setSystemStatusOpen(false)}
        footer={null}
        width={720}
      >
        <Space align="center" wrap style={{ marginBottom: 12 }}>
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={systemStatsLoading}
            onClick={() => refreshSystemStats()}
          >
            刷新
          </Button>
          {llmHealth?.preferred_provider && (
            <Tag color="blue">推荐: {llmHealth.preferred_provider}</Tag>
          )}
        </Space>
        {llmHealth?.providers?.length ? (
          <div style={{ marginBottom: 12 }}>
            <Text type="secondary">Provider 状态</Text>
            <Space wrap style={{ marginTop: 6 }}>
              {llmHealth.providers.map((provider) => {
                const inCooldown = provider.in_cooldown
                const color = !provider.available
                  ? 'default'
                  : inCooldown
                    ? 'orange'
                    : 'green'
                const cooldown = provider.cooldown_remaining_seconds || 0
                const tooltipText = provider.last_error
                  ? `失败 ${provider.failures || 0} 次：${provider.last_error}`
                  : inCooldown
                    ? `冷却中剩余 ${cooldown}s`
                    : '正常'
                return (
                  <Tooltip key={provider.provider} title={tooltipText}>
                    <Tag color={color}>
                      {provider.provider}
                      {inCooldown ? ` 冷却${cooldown}s` : ' OK'}
                    </Tag>
                  </Tooltip>
                )
              })}
            </Space>
          </div>
        ) : null}
        <div style={{ marginBottom: 12 }}>
          <Text type="secondary">Token / 成本</Text>
          <Space wrap style={{ marginTop: 6 }}>
            <Tag color="geekblue">Tokens 总计: {llmTotals.tokens.toLocaleString()}</Tag>
            <Tag color="purple">费用总计: {llmTotals.cost.toFixed(6)}</Tag>
          </Space>
        </div>
        {llmMetricEntries.length > 0 && (
          <div>
            <Text type="secondary">模型统计</Text>
            <Space wrap style={{ marginTop: 6 }}>
              {llmMetricEntries.map((entry) => (
                <Tooltip
                  key={entry.key}
                  title={`成功 ${entry.success} / 失败 ${entry.failure} | tokens ${entry.total_tokens} | cost ${entry.total_cost.toFixed(6)}`}
                >
                  <Tag color={entry.failure ? 'volcano' : 'default'}>
                    {entry.provider}/{entry.model}
                  </Tag>
                </Tooltip>
              ))}
            </Space>
          </div>
        )}
      </Modal>
      
      {contextMenuVisible && (
        <div
          className="doc-studio__context-menu"
          style={{
            left: contextMenuPosition.x,
            top: contextMenuPosition.y,
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {contextMenuActions.map((action) => (
            <button
              key={action.key}
              type="button"
              className={`doc-studio__context-menu-item ${
                action.danger ? 'doc-studio__context-menu-item--danger' : ''
              } ${action.separated ? 'doc-studio__context-menu-item--separated' : ''}`}
              disabled={action.disabled}
              onClick={() => {
                if (action.disabled) return
                action.onClick()
              }}
            >
              <span className="doc-studio__context-menu-icon">{action.icon}</span>
              <span className="doc-studio__context-menu-label">{action.label}</span>
            </button>
          ))}
        </div>
      )}

      <Modal
        title="Agent 消息原始输出调试"
        open={debugModalOpen}
        onCancel={() => setDebugModalOpen(false)}
        footer={null}
        width={720}
      >
        {debugData?.error ? (
          <Text type="danger">{debugData.error}</Text>
        ) : debugData?.items?.length ? (
          <div style={{ maxHeight: 400, overflow: 'auto' }}>
            {debugData.items.map((item, i) => (
              <div key={item.message_id || i} style={{ marginBottom: 16, padding: 12, background: '#fafafa', borderRadius: 8 }}>
                <Space size={[8, 8]} wrap>
                  <Tag>消息 {i + 1}</Tag>
                  <Tag>长度 {item.content_length}</Tag>
                  <Tag color="blue">\\n × {item.newline_count}</Tag>
                  <Tag color="green">\\n\\n × {item.double_newline_count}</Tag>
                  <Tag color={item.triple_plus_newline_count > 0 ? 'red' : 'default'}>3+换行 × {item.triple_plus_newline_count}</Tag>
                </Space>
                <pre style={{ marginTop: 8, fontSize: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                  {item.raw_with_markers}
                </pre>
                <Text type="secondary" style={{ fontSize: 11 }}>repr 样本: {item.raw_repr_sample}</Text>
              </div>
            ))}
          </div>
        ) : (
          <Text type="secondary">暂无 Agent 消息或加载失败</Text>
        )}
      </Modal>
    </>
  )
}

export default LatexEditorPage


