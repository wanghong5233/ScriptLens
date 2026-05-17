import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
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
  Progress,
  Radio,
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
import ScriptlensReportRail from './component/scriptlens-report-rail'
import {
  buildPromptFromTask,
  highlightLineRange,
  type AgentTask,
  type DimensionKey,
} from './agentTask'
import { RewritePlanCard, type RewritePlanData } from './RewritePlanCard'
import { SCRIPTLENS_LIGHT_THEME } from './monacoTheme'
import { ChatMarkdown } from '@/components/markdown/ChatMarkdown'
import Recorder from '@/components/sender/recorder'
import { fetchLlmModels, type LlmModelCatalog } from '@/api/config'
import type React from 'react'
import type { TextAreaRef } from 'antd/es/input/TextArea'
import {
  compileWorkspace,
  createFileOrDirectory,
  createWorkspace,
  deleteWorkspace,
  fetchWorkspace,
  reanalyzeScript,
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
  findSceneById,
  resolveScenePathAliases,
  exportFullScript,
  type ScriptExportFormat,
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

const SCRIPT_PROCESSING_STATUS = new Set(['pending', 'parsing', 'indexing'])
const SCRIPT_STATUS_LABELS: Record<string, string> = {
  pending: '排队中',
  parsing: '文本解析与集场切分',
  indexing: '检索索引入库',
  ready: '解析完成',
  failed: '解析失败',
}
const SCRIPT_STATUS_PROGRESS: Record<string, number> = {
  pending: 12,
  parsing: 46,
  indexing: 78,
  ready: 100,
  failed: 100,
}

function inferEpisodeUpperBoundFromTitle(title: string): number {
  const match = title.match(/(?:第)?\s*\d+\s*[-~—至到]\s*(\d+)\s*(?:集|话|回)?/)
  if (!match) return 0
  const upper = Number(match[1])
  return Number.isFinite(upper) ? upper : 0
}

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
  const detail = error?.response?.data?.detail
  if (detail && typeof detail === 'object') {
    return detail?.message || detail?.code || '请求失败'
  }
  return (
    detail ||
    error?.response?.data?.message ||
    error?.message ||
    '请求失败'
  )
}

const getErrorCode = (error: any): string => {
  const code = error?.response?.data?.detail?.code
  return typeof code === 'string' ? code : ''
}

const getOperationErrorMessage = (error: any, fallbackPrefix = '操作失败') => {
  const code = getErrorCode(error)
  if (code === 'SCRIPT_NOT_FOUND') return '当前剧本不存在或你没有访问权限'
  if (code === 'SCRIPT_NOT_READY') return '剧本尚未就绪，请稍后重试'
  if (code === 'REPORT_NOT_READY') return '评分报告正在生成，请稍后刷新'
  if (code === 'SCENE_NOT_FOUND') return '目标场景不存在或你没有访问权限'
  if (code === 'INVALID_SCENE_CONTENT') return '场景内容不合法，请检查后重试'
  if (code === 'OPERATION_NOT_FOUND') return '该操作记录不存在，可能已过期'
  if (code === 'OPERATION_FORBIDDEN') return '你没有权限访问该操作记录'
  if (code === 'INVALID_OPERATION_REQUEST') return '操作请求格式非法，请刷新后重试'
  if (code === 'INVALID_FEEDBACK_REQUEST') return '反馈参数不合法，请检查后重试'
  if (code === 'SCRIPT_FORBIDDEN') return '你没有权限访问该剧本'
  if (code === 'INVALID_EXPORT_REQUEST') return '导出参数不合法，请检查后重试'
  if (code === 'REWRITE_FAILED') return '改写失败，请调整指令后重试'
  return `${fallbackPrefix}: ${getErrorMessage(error)}`
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

/**
 * D1（左栏 C 方案）：完整剧本视图的虚拟 path 标识。
 * 真实 scene_id 是 UUID，不会跟它撞；后端任何接口都不会接受这个 path。
 * openFile 与 Editor readOnly 在前端各自拦截。
 */
const FULL_SCRIPT_VIRTUAL_PATH = '__SCRIPTLENS_FULL_SCRIPT__'

/**
 * 在文本里精确定位 quote 的 1-based 物理行号范围。
 *
 * 业内对照（Hypothes.is W3C TextQuoteSelector / Notion / GitHub PR）：
 * 引用应以原文片段为 ground truth，行号仅作加速。直接 indexOf 优先；
 * 命中失败时退化到「逐行子串包含」（覆盖 quote 是某行子串的常见情况：
 * 比如 LLM 截了句号但原文行末是逗号）。仍找不到 → null，调用方走 fallback。
 *
 * 不使用复杂 fuzzy match（编辑距离 / token 对齐）：剧本是结构化纯文本，
 * 后端 quote 是直接 split("\\n") 取的真实 line，indexOf 精确匹配率应 ≥ 95%；
 * 真有偏差大概率是 LLM 改写了 quote（属于后端 bug，前端不应掩盖）。
 */
function findQuoteRangeInText(
  text: string,
  quote: string,
): { start: number; end: number } | null {
  const trimmedQuote = quote.trim()
  if (!trimmedQuote || !text) return null

  // 策略 1：原样 indexOf（最准）
  const idx = text.indexOf(trimmedQuote)
  if (idx >= 0) {
    const before = text.slice(0, idx)
    const startLine = before.split('\n').length
    const linesInQuote = trimmedQuote.split('\n').length
    return { start: startLine, end: startLine + linesInQuote - 1 }
  }

  // 策略 2：逐行子串包含（quote 是某行子串的兜底，常见于 LLM 截标点）
  const lines = text.split('\n')
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].includes(trimmedQuote)) {
      return { start: i + 1, end: i + 1 }
    }
  }

  // 策略 3：取 quote 前 20 字（去标点 / 空白）逐行子串模糊匹配
  const head = trimmedQuote.replace(/[\s\p{P}]/gu, '').slice(0, 20)
  if (head.length >= 6) {
    for (let i = 0; i < lines.length; i += 1) {
      const lineNorm = lines[i].replace(/[\s\p{P}]/gu, '')
      if (lineNorm.includes(head)) {
        return { start: i + 1, end: i + 1 }
      }
    }
  }

  return null
}

/**
 * 在场次树顶部插入「📖 完整剧本」虚拟节点。
 * 显示「集数 / 场数」帮用户一眼看到剧本规模。
 */
const wrapWithFullScriptNode = (
  files: DocStudioAPI.FileNode[] | undefined,
): DocStudioAPI.FileNode[] => {
  if (!files?.length) return files || []
  let scenes = 0
  const episodes = new Set<string>()
  const walk = (nodes: DocStudioAPI.FileNode[]) => {
    for (const n of nodes) {
      if (n.type === 'directory') {
        episodes.add(n.path)
        if (n.children?.length) walk(n.children)
      } else {
        scenes += 1
      }
    }
  }
  walk(files)
  // 单集摊平的场景：scenesToFileTree 不会建 directory，此时按 1 集统计
  const epCount = episodes.size || (scenes > 0 ? 1 : 0)
  const fullNode: DocStudioAPI.FileNode = {
    name: `📖 完整剧本（${epCount} 集 · ${scenes} 场）`,
    path: FULL_SCRIPT_VIRTUAL_PATH,
    type: 'file',
  }
  return [fullNode, ...files]
}

const LIVE_TOOL_LABELS: Record<string, string> = {
  // ScriptLens 自有工具（短剧分析）：放最前面，命中频次最高
  score_dimension_tool: '维度评分',
  locate_scenes_tool: '定位场次',
  extract_characters_tool: '抽取人物',
  rewrite_selection_scene_tool: '改写选区',
  propose_rewrite_tool: '改写建议',
  // 复用 ScholarMind 通用工具：保留可显示，不破坏底层能力
  analyze_context_tool: '上下文分析',
  analyze_document_tool: '文档分析',
  semantic_code_search_tool: '语义检索',
  search_codebase_tool: '原文检索',
  read_file_range_tool: '按行读取',
  list_workspace_tree_tool: '浏览场次',
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
  compile_latex_tool: '重新诊断',
  check_citation_consistency_tool: '检查引用一致性',
  check_bibliography_tool: '检查参考文献',
  web_search_tool: '联网检索',
  reply_to_user_tool: '生成最终回复',
  answer_without_edit_tool: '直接回答',
}

const formatLiveToolName = (toolName?: string) => {
  const normalized = String(toolName || '').trim()
  if (!normalized) return ''
  if (LIVE_TOOL_LABELS[normalized]) return LIVE_TOOL_LABELS[normalized]
  return normalized.replace(/_tool$/, '').replace(/_/g, ' ')
}

const formatOperationIdForDisplay = (operationId?: string) => {
  const raw = String(operationId || '').trim()
  if (!raw) return ''
  const colonIndex = raw.indexOf(':')
  const payload = colonIndex >= 0 ? raw.slice(colonIndex + 1) : raw
  return payload.slice(0, 8)
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
// ScriptLens 场景：@file 占位符在显示层重命名为 @scene（一份剧本=一份"文件"，每个场次=一个子节点）
// 保留 JS 标识符 fileMentions/FileMentionFragment 不变，只动序列化字符串，避免破坏底层数据结构
const FILE_PLACEHOLDER_REGEX = /@scene\d+/g
const COMPOSER_PLACEHOLDER_REGEX = /@(selection|scene)\d+/g
const containsSelectionPlaceholder = (value: string) =>
  new RegExp(SELECTION_PLACEHOLDER_REGEX.source).test(String(value || ''))
const containsFilePlaceholder = (value: string) =>
  new RegExp(FILE_PLACEHOLDER_REGEX.source).test(String(value || ''))

const normalizeSelectionPlaceholder = (value: unknown, fallbackIndex: number) => {
  const raw = String(value || '').trim()
  if (/^@selection\d+$/i.test(raw)) return raw
  return `@selection${fallbackIndex + 1}`
}

const escapeSelectionText = (value: string): string =>
  String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')

const wrapSelectionFragment = (fragment: SelectionFragment): string => {
  const attrs: string[] = [`id="${fragment.placeholder}"`]
  if (fragment.filePath) attrs.push(`file_path="${fragment.filePath.replace(/"/g, "'")}"`)
  if (fragment.startLine && fragment.endLine) {
    attrs.push(`line_range="${fragment.startLine}-${fragment.endLine}"`)
  }
  return `<SELECTION ${attrs.join(' ')}>\n${escapeSelectionText(fragment.text)}\n</SELECTION>`
}

const injectSelectionBlocksIntoPrompt = (
  promptText: string,
  selectionFragments: SelectionFragment[],
): string => {
  let output = String(promptText || '')
  for (const fragment of selectionFragments) {
    const placeholder = String(fragment.placeholder || '').trim()
    if (!placeholder) continue
    const pattern = new RegExp(`${escapeRegExp(placeholder)}(?!\\d)`, 'g')
    output = output.replace(pattern, wrapSelectionFragment(fragment))
  }
  return output
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
  if (/^@scene\d+$/i.test(raw)) return raw
  return `@scene${fallbackIndex + 1}`
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
    const isFileMention = placeholder.startsWith('@scene')
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
  { label: 'qwen3-max-latest', value: 'qwen3-max-latest' },
  { label: 'qwen-max-latest', value: 'qwen-max-latest' },
  { label: 'qwen-max', value: 'qwen-max' },
  { label: 'qwen3-max', value: 'qwen3-max' },
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

const DEFAULT_DASHSCOPE_MODEL = 'qwen3-max-latest'
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
const BLOCKED_RUNTIME_LLM_MODELS = new Set(['qwen-plus', 'qwen-turbo', 'qwen2.5-plus'])
const buildLlmModelOptionsFromCatalog = (
  catalog: LlmModelCatalog | null,
): LlmModelOption[] => {
  const remote = (catalog?.models ?? [])
    .filter((item) => item.provider === 'dashscope' || item.provider === 'openai')
    .filter((item) => !BLOCKED_RUNTIME_LLM_MODELS.has(String(item.model || '').trim().toLowerCase()))
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
// ScriptLens 调宽（与 ScholarMind 默认值不同）：
// 报告是主要阅读区，不是窄 chat 辅助栏；最大宽度不要替用户做死决定。
const MIN_LEFT_SIDER_WIDTH = 200
const MAX_LEFT_SIDER_WIDTH = 600
const MIN_RIGHT_SIDER_WIDTH = 380
const MAX_RIGHT_SIDER_WIDTH = 1600
const MIN_CENTER_WIDTH = 240

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
  const location = useLocation()
  const snap = useSnapshot(docStudioState)
  const [prompt, setPrompt] = useState('')
  const [chatLoading, setChatLoading] = useState(false)
  const [reEditDraft, setReEditDraft] = useState<ReEditDraft | null>(null)
  const [reEditSubmitting, setReEditSubmitting] = useState(false)
  const [activeScriptDetail, setActiveScriptDetail] = useState<DocStudioAPI.WorkspaceDetail | null>(null)
  const [scriptStatusPolling, setScriptStatusPolling] = useState(false)
  const [scriptProcessingStartedAt, setScriptProcessingStartedAt] = useState<number | null>(null)
  const [scriptElapsedNow, setScriptElapsedNow] = useState(() => Date.now())
  const asyncMode = true
  const [selections, setSelections] = useState<SelectionFragment[]>([])
  const [fileMentions, setFileMentions] = useState<FileMentionFragment[]>([])
  const [fileMentionQuery, setFileMentionQuery] = useState('')
  const [fileMentionRange, setFileMentionRange] = useState<{ start: number; end: number } | null>(null)
  const [fileMentionActiveIndex, setFileMentionActiveIndex] = useState(0)
  const [workspaceModalOpen, setWorkspaceModalOpen] = useState(false)
  const [workspaceDeleting, setWorkspaceDeleting] = useState(false)
  // F2：导出完整剧本 Modal（仅 ScriptLens 场景）
  const [exportModalOpen, setExportModalOpen] = useState(false)
  const [exportFormat, setExportFormat] = useState<ScriptExportFormat>('docx')
  const [exporting, setExporting] = useState(false)
  const [newWorkspaceName, setNewWorkspaceName] = useState('')
  // ScriptLens 不区分 latex / markdown，但 setter 还在 onCancel/重置流程里用到，
  // 留个 _ 前缀的占位避免 TS noUnusedLocals 报错。
  const [, setNewWorkspaceType] = useState<'latex' | 'markdown'>('latex')
  const [workspaceSubmitting, setWorkspaceSubmitting] = useState(false)
  // ScriptLens 上传单文件即创建剧本工作区
  const [newWorkspaceFile, setNewWorkspaceFile] = useState<File | null>(null)
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
  const [rightTab, setRightTab] = useState<'chat' | 'history' | 'compile' | 'report'>(() => {
    if (typeof window === 'undefined') return 'chat'
    const saved = localStorage.getItem('doc_studio_right_tab')
    return (saved === 'chat' || saved === 'history' || saved === 'compile' || saved === 'report') ? saved : 'chat'
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
      const normalizedValue = typeof value === 'string' ? value.trim().toLowerCase() : ''
      if (normalizedValue && BLOCKED_RUNTIME_LLM_MODELS.has(normalizedValue)) {
        return defaultRuntimeModelByProvider('dashscope')
      }
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
  const autoCompileHandledRef = useRef(false)
  const scriptReadyRefreshRef = useRef<Record<string, true>>({})
  const autoOpenedReportForScriptRef = useRef<Record<string, true>>({})
  
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
  // fe_rescore_hook（docs/10-rewrite-agent.md §7）：dispatch fulltext_rewrite execute 时
  // 把待评分维度记进来；最后一个 hunk 被 keep（closeDiffModal 走 contentByPath 分支）后
  // 自动追发一条 rescore 用户消息。reject all 路径不触发；reset 在 close 之后。
  const pendingRescoreRef = useRef<DimensionKey[] | null>(null)
  // closeDiffModal 在文件靠后位置定义，但需要回调 dispatchAgentTask；用 ref 解循环依赖。
  const dispatchAgentTaskRef = useRef<
    ((task: AgentTask, options?: { autoSubmit?: boolean }) => Promise<void>) | null
  >(null)
  const asyncRunResolvedRef = useRef(false)
  // Bug 5 兜底：finish 后 livePreview 已流完但 result 迟迟不来时，
  // 把 livePreview 提前作为 chat reply 提交并关 spinner；
  // result 真到达时 handleAgentResponse 通过该 ref 跳过重复 pushChatMessage，
  // 但仍正常处理 file_diffs / operationId / executionHistory 等副作用。
  const livePromotedToChatRef = useRef(false)
  // setTimeout closure 捕获过期 state，用 ref 镜像 livePreview 文本以便兜底读到最新值。
  const liveAgentPreviewTextRef = useRef('')
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
    // ScriptLens 默认 560：报告需要同时承载判断、证据、主线、人物与改写入口。
    // 旧值过窄时回升，避免用户每次打开都被 360px 的 ScholarMind 旧习惯卡住。
    if (saved) {
      const parsed = parseInt(saved, 10)
      if (Number.isFinite(parsed) && parsed >= 460) return parsed
    }
    return 560
  })
  
  const [isDraggingLeft, setIsDraggingLeft] = useState(false)
  const [isDraggingRight, setIsDraggingRight] = useState(false)
  
  const preferredKbFromUrl = useMemo(() => {
    const raw = new URLSearchParams(location.search).get('kb_id')
    if (!raw) return null
    const parsed = Number(raw)
    return Number.isFinite(parsed) ? parsed : null
  }, [location.search])

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
    asyncStreamRef.current?.close()
    asyncStreamRef.current = null
    diffHunkDecorationsRef.current = []
    diffEditorListenerRef.current.forEach((listener) => {
      try {
        listener.dispose()
      } catch (error) {
        console.warn('Failed to dispose diff listener while switching workspace', error)
      }
    })
    diffEditorListenerRef.current = []
    pendingSendRef.current = null
    stopRequestedRef.current = true
    asyncRunResolvedRef.current = false
    liveDeltaStartedRef.current = false
    seenLiveEventIdsRef.current = new Set()
    handledInteractionIdsRef.current = new Set()
    lastLiveEventSequenceRef.current = -1
    activeRunIdRef.current = null

    setPrompt('')
    setChatLoading(false)
    setReEditDraft(null)
    setReEditSubmitting(false)
    setOperationHistory([])
    setOperationHistoryLoading(false)
    setAgentDiffReviewOpen(false)
    setDiffModalOpen(false)
    setAllFileDiffs([])
    setCurrentDiffIndex(0)
    setLastOperationId(null)
    setDiffOperationId(null)
    setDiffModalContext('agent')
    setUndoingLastApply(false)
    setDiffReverting(false)
    setRevertingOperationId(null)
    setResolvedOriginal('')
    setResolvedModified('')
    setLineChanges([])
    setCurrentHunkIndex(0)
    setLiveAgentStatus('')
    setLiveAgentTimeline([])
    setLiveAgentPreviewText('')
    setLiveDeltaCharCount(0)
    setLiveAgentElapsedSec(0)
    autoCompileHandledRef.current = false
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
    const raw = new URLSearchParams(location.search).get('file')
    return raw ? raw.trim() : ''
  }, [location.search])
  const autoCompileFromUrl = useMemo(() => {
    const raw = new URLSearchParams(location.search).get('auto_compile')
    if (!raw) return false
    const normalized = raw.trim().toLowerCase()
    return normalized === '1' || normalized === 'true' || normalized === 'yes'
  }, [location.search])
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
  // ScriptLens 场景：剧本上传后 path 是 UUID（无扩展名），既不是 latex / markdown / txt
  // 编译动作改为「重新诊断」—— 触发 reanalyze，重跑 5 维评分
  const isScriptLensSession = useMemo(
    () =>
      !!snap.workspaceId &&
      !isLatexWorkspace &&
      !isMarkdownActiveFile &&
      !isPlaintextActiveFile,
    [snap.workspaceId, isLatexWorkspace, isMarkdownActiveFile, isPlaintextActiveFile],
  )
  const scriptStatus = useMemo(
    () =>
      String(
        activeScriptDetail?.config?.status ||
        snap.workspaceConfig?.status ||
        '',
      ).trim(),
    [activeScriptDetail?.config, snap.workspaceConfig],
  )
  const isScriptProcessing = isScriptLensSession && SCRIPT_PROCESSING_STATUS.has(scriptStatus)
  const isScriptFailed = isScriptLensSession && scriptStatus === 'failed'
  const scriptProcessingProgress = SCRIPT_STATUS_PROGRESS[scriptStatus] ?? (isScriptProcessing ? 35 : 0)
  const scriptProcessingElapsedSec = scriptProcessingStartedAt
    ? Math.max(0, Math.floor((scriptElapsedNow - scriptProcessingStartedAt) / 1000))
    : 0
  const scriptProcessingIsLarge = Number(
    activeScriptDetail?.config?.total_episodes ||
    snap.workspaceConfig?.total_episodes ||
    0,
  ) >= 80 ||
    Number(activeScriptDetail?.config?.total_scenes || snap.workspaceConfig?.total_scenes || 0) >= 150 ||
    Number(activeScriptDetail?.config?.total_chars || snap.workspaceConfig?.total_chars || 0) >= 50000 ||
    inferEpisodeUpperBoundFromTitle(activeWorkspaceName) >= 80

  // syncWorkspaceFileTree 在下面才声明，但 handleScriptDetailLoaded 需要引用它来在
  // detail.status=ready 时立即同步场景树（解决"报告轮询比 pollScriptStatus 快、左栏
  // 卡在 stale fileTree"的 race）。用 ref 间接引用：声明顺序无关，每次 render 同步赋值。
  const syncFileTreeRef = useRef<((id: string) => Promise<void>) | null>(null)

  const handleScriptDetailLoaded = useCallback((detail: {
    id: string
    title: string
    source_format?: string | null
    status: string
    total_episodes?: number | null
    total_scenes?: number | null
    total_chars?: number | null
    failure_reason?: string | null
  }) => {
    const config = {
      ...(docStudioState.workspaceConfig || {}),
      title: detail.title,
      source_format: detail.source_format,
      status: detail.status,
      total_episodes: detail.total_episodes,
      total_scenes: detail.total_scenes,
      total_chars: detail.total_chars,
      failure_reason: detail.failure_reason,
    }
    setActiveScriptDetail({
      workspaceId: detail.id,
      name: detail.title,
      mainFile: undefined,
      fileCount: detail.total_scenes ?? 0,
      updatedAt: Date.now(),
      config,
    })
    docStudioActions.setWorkspaceConfig(config)

    // ⚠️ 关键：报告流程（ScriptlensReportProgress）跟 pollScriptStatus 是两条独立轮询，
    // 报告通常先就绪、status 慢一拍刷到 ready。这里以"报告 detail 拿到 status=ready"
    // 为信号立即 sync 场景树，避免左栏卡在上一份剧本的 stale fileTree。
    // 用 scriptReadyRefreshRef 去重——避免多次 mount/切 tab 反复拉。
    if (
      detail.status === 'ready' &&
      detail.id &&
      !scriptReadyRefreshRef.current[detail.id]
    ) {
      scriptReadyRefreshRef.current[detail.id] = true
      void syncFileTreeRef.current?.(detail.id)
    }
  }, [])

  useEffect(() => {
    if (!isScriptProcessing) return
    const timer = window.setInterval(() => setScriptElapsedNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [isScriptProcessing])
  const supportsCompilePanel = isLatexWorkspace || isMarkdownActiveFile || isScriptLensSession
  const compileActionTitle = useMemo(() => {
    if (isScriptLensSession) return '重新诊断（基于已修改场次重跑 5 维评分）'
    if (isPlaintextActiveFile) return 'TXT 文件无需编译'
    if (isMarkdownActiveFile) return '编译 Markdown'
    return '编译'
  }, [isScriptLensSession, isMarkdownActiveFile, isPlaintextActiveFile])

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

  /**
   * F1：阅文五力改写快捷指令模板。Key 与后端 RewriteRequest.target_dimension 一致
   * （docs/08-evaluation-framework.md §3）。compliance 不出现在改写菜单——红线问题
   * 由专门的"降风险"专项工具处理，不走普通改写通道。
   *
   * 提示词只描述「任务」，不描述「呈现」——diff / Keep / Undo 是编辑器自身机制
   * （propose_rewrite_tool 输出的 result 由前端 chat 流接到 AgentDiffReview，跟 LLM 无关）。
   * 强调「结合整本剧 / 前后场次 / 主线走向」是为了让 Agent 主动去 read_scene_tool
   * 拉相邻场上下文，而非只看一场就改——剧本是连贯逻辑。
   */
  const REWRITE_QUICK_COMMANDS: Record<string, { label: string; prompt: string }> = useMemo(
    () => ({
      story: {
        label: '改故事',
        prompt: '请针对「故事力」维度（主线清晰度 + 反转密度 + 情节因果），结合整本剧人物关系与主线走向，定位最弱的场次并给出具体改写建议。',
      },
      character: {
        label: '改人物',
        prompt: '请针对「人物力」维度（主角动机弧光 + 关键关系冲突），结合前后场次的铺垫与回收，定位人物塌陷的场次并给出具体改写建议。',
      },
      concept: {
        label: '改题材',
        prompt: '请针对「题材力」维度（赛道辨识度 + 卖点钩子 + 用户画像匹配），结合短剧市场常见赛道，定位题材模糊或卖点稀薄的场次并给出具体改写建议。',
      },
      emotion: {
        label: '改情感',
        prompt: '请针对「情感力」维度（情感钩子 + 爽点密度 + 共鸣点），结合前后场次的情绪曲线，定位干瘪 / 塌陷的场次并给出具体改写建议。',
      },
      pacing: {
        label: '改节奏',
        prompt: '请针对「叙事力」维度（开场速度 + 节奏方差 + 中段密度），结合整本剧的节奏曲线，定位拖沓 / 跳跃的场次并给出具体改写建议。',
      },
    }),
    [],
  )

  const fillRewritePromptByDimension = useCallback(
    (dimension: string) => {
      const cmd = REWRITE_QUICK_COMMANDS[dimension]
      if (!cmd) return
      setPrompt(cmd.prompt)
      requestAnimationFrame(() => {
        promptInputDivRef.current?.focus()
      })
      message.info(`已预填「${cmd.label}」指令到对话框，按 Enter 发送`, 2.5)
    },
    [REWRITE_QUICK_COMMANDS],
  )

  // F1(a) 报告页跳转过来：解析 ?prefill_dim=xxx → 预填 chat 指令 + 清掉 query
  useEffect(() => {
    if (typeof window === 'undefined') return
    if (!snap.workspaceId) return
    const url = new URL(window.location.href)
    const dim = url.searchParams.get('prefill_dim')
    if (!dim || !REWRITE_QUICK_COMMANDS[dim]) return
    fillRewritePromptByDimension(dim)
    url.searchParams.delete('prefill_dim')
    const nextSearch = url.searchParams.toString()
    window.history.replaceState(
      {},
      '',
      `${url.pathname}${nextSearch ? `?${nextSearch}` : ''}${url.hash}`,
    )
  }, [snap.workspaceId, REWRITE_QUICK_COMMANDS, fillRewritePromptByDimension])


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
    () => {
      // ScriptLens 场景：path 是场次 uuid（如 39d64ef9-...），人类可读名（"第 5 集 5-3 场《沈宅 夜 内》"）
      // 存在 fileTree.node.name 上。优先从场次树查显示名，找不到才兜底为 path 末段。
      const lookupName = (nodes: typeof snap.fileTree, target: string): string | null => {
        if (!nodes || !Array.isArray(nodes)) return null
        for (const node of nodes as Array<{ path: string; name: string; children?: any[] }>) {
          if (node.path === target) return node.name
          if (node.children) {
            const found = lookupName(node.children as any, target)
            if (found) return found
          }
        }
        return null
      }
      return snap.openedFiles.map((path) => {
        const displayName =
          path === FULL_SCRIPT_VIRTUAL_PATH
            ? '📖 完整剧本'
            : lookupName(snap.fileTree, path) || path.split('/').pop() || path
        return {
          key: path,
          label: (
            <span className="doc-studio__header-tab-label" title={displayName}>
              {displayName}
            </span>
          ),
        }
      })
    },
    [snap.openedFiles, snap.fileTree],
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
        const newPlaceholder = `@scene${idx + 1}`
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
          message.warning(`最多可引用 ${MAX_FILE_MENTION_COUNT} 个场次`)
          return
        }
        placeholder = `@scene${fileMentions.length + 1}`
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
      if (placeholder.startsWith('@scene')) {
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
      const tagClass = match.startsWith('@scene')
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
    if (!snap.activeFilePath || !snap.workspaceId) return []
    const activeAliases = resolveScenePathAliases(snap.workspaceId, snap.activeFilePath)
    const normalizedActiveAliases = new Set(
      (activeAliases.length ? activeAliases : [snap.activeFilePath]).map((p) =>
        normalizeWorkspacePath(p),
      ),
    )
    const list = operationHistory.filter((item) =>
      Array.isArray(item.modified_files) &&
      item.modified_files.some(
        (filePath) => {
          const raw = String(filePath || '')
          const aliases = resolveScenePathAliases(snap.workspaceId, raw)
          const candidates = aliases.length ? aliases : [raw]
          return candidates.some((candidate) =>
            normalizedActiveAliases.has(normalizeWorkspacePath(candidate)),
          )
        },
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
  }, [operationHistory, snap.activeFilePath, snap.workspaceId])

  const openFile = useCallback(
    async (path: string, forceReload = false, silent = false) => {
      const workspaceIdAtStart = docStudioState.workspaceId
      if (!workspaceIdAtStart) return
      const isWorkspaceStillCurrent = () => docStudioState.workspaceId === workspaceIdAtStart
      // D1：「完整剧本」虚拟视图——前端按 fileTree 顺序拼接所有场次内容。
      // 后端无此 path，绝对不能走 fetchFileContent。
      if (path === FULL_SCRIPT_VIRTUAL_PATH) {
        const existing = docStudioState.files[path]
        if (!forceReload && existing && !existing.loading) {
          docStudioActions.setActiveFile(path)
          return
        }
        docStudioActions.setActiveFile(path)
        if (!silent) docStudioActions.setFileLoading(path, true)
        try {
          const orderedScenePaths = collectAllFilePaths(
            (docStudioState.fileTree || []) as DocStudioAPI.FileNode[],
          )
          if (orderedScenePaths.length === 0) {
            docStudioActions.setFileContent(path, '（剧本暂无场次）', 'utf-8')
            return
          }
          const parts: string[] = []
          for (const scenePath of orderedScenePaths) {
            const buf = docStudioState.files[scenePath]
            if (buf?.content) {
              parts.push(buf.content)
              continue
            }
            // eslint-disable-next-line no-await-in-loop
            const file = await fetchFileContent(
              { workspaceId: workspaceIdAtStart, path: scenePath },
              { loading: false, errorToast: false },
            )
            if (!isWorkspaceStillCurrent()) return
            docStudioActions.setFileContent(scenePath, file.content, file.encoding)
            parts.push(file.content || '')
          }
          if (!isWorkspaceStillCurrent()) return
          docStudioActions.setFileContent(path, parts.join('\n\n'), 'utf-8')
        } catch (error) {
          if (!isWorkspaceStillCurrent()) return
          showRequestError(error)
        } finally {
          if (isWorkspaceStillCurrent()) {
            docStudioActions.setFileLoading(path, false)
          }
        }
        return
      }
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
          workspaceId: workspaceIdAtStart,
          path,
        }, {
          loading: false,
          errorToast: false,
        })
        if (!isWorkspaceStillCurrent()) return
        docStudioActions.setFileContent(path, file.content, file.encoding)
      } catch (error) {
        if (!isWorkspaceStillCurrent()) return
        showRequestError(error)
      } finally {
        if (isWorkspaceStillCurrent()) {
          docStudioActions.setFileLoading(path, false)
        }
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

  // F1 改写入口最终选择「chat 预填指令」路径（见 fillRewritePromptByDimension）：
  //   - 走 ReAct Agent，自动 locate_scenes_tool → propose_rewrite_tool；
  //   - 用户能在 chat 里继续追问「再激进一点」「保留女主线」等迭代式改写；
  //   - 与 PRD §8 持续追问 / 持续改写的核心理念一致；
  // 不再保留直接调 POST /rewrite 的捷径函数——避免「按钮一键改写后却看不到推理过程」
  // 的"假 Agent"体验。chat 里的 propose_rewrite_tool 仍能产出 diff 走 AgentDiffReview。

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

  // ========== 报告 ↔ 编辑器联动：两类语义严格区分 ==========
  //
  // 1) traceEvidence(溯源)：点 evidence chip / 关键场景 / 主要看点
  //    → openFile + Monaco **持久高亮**（直到点别处 / 切剧本 / 显式取消）
  //    → rail 同步设置 activeEvidenceId，UI 同色高亮对齐
  //    → **不切 chat tab、不注入 prompt、不派 Agent**
  //    （这是 task.md §三-2"保留原文依据"的核心：用户要"对齐论点和论据"）
  //
  // 2) dispatchAgentTask(派 Agent)：点"让 Agent 改写" / 维度追问按钮
  //    → 切 chat tab + 注入 prompt（rewrite_seed / dim_inquiry）
  //    → 同时也跳一下编辑器但**3 秒淡出**（视线引导，避免污染编辑区）
  //
  // 旧版本里所有点击都走 dispatchAgentTask，相当于"看一眼原文"也被强行扔到 Agent 对话；
  // 用户反馈这是"狗屎跳转"——溯源就该是只读对齐，不该被 LLM 介入。

  const [activeEvidenceId, setActiveEvidenceId] = useState<string | null>(null)
  const traceDisposeRef = useRef<(() => void) | null>(null)

  /** 显式清掉当前持久高亮（切剧本 / rail 主动取消时调用）。 */
  const clearEvidenceTrace = useCallback(() => {
    if (traceDisposeRef.current) {
      try {
        traceDisposeRef.current()
      } catch {
        // 编辑器已销毁等情况：静默
      }
      traceDisposeRef.current = null
    }
    setActiveEvidenceId(null)
  }, [])

  // ============================================================
  // v3.3 Line-range anchored 溯源
  // ============================================================
  /**
   * 溯源跳原文 + 持久高亮。
   *
   * 业内对照（GitHub PR review hunk / Cursor codebase index / NotebookLM citation /
   * Hypothesis W3C TextPositionSelector）：line_range 是 primary anchor，quote
   * 字符串只作 fallback，**绝不**让 quote 字符匹配主导跳转。
   *
   * 优先级（推倒重做后的统一基础设施）：
   *   1. (startLine, endLine) ← LLM 同次给的 evidence_line_range  → 直接高亮该区间
   *   2. quote 字符串 fallback → 在 model 文本里 indexOf 找位置（旧契约 / line_range 缺失时救火）
   *   3. 都没有 → 高亮整场（用户至少能看到目标 scene 内容，比白屏好）
   *
   * Editor 用 key={activeFilePath} unmount+remount，model.getValue() === scene.text
   * 强校验保证不命中旧文件 model。
   */
  const traceEvidence = useCallback(
    async (params: {
      evidenceRefId: string | null
      sceneId: string
      startLine?: number | null
      endLine?: number | null
      quote?: string | null
    }): Promise<void> => {
      const workspaceId = docStudioState.workspaceId
      if (!workspaceId) {
        message.warning('请先选择剧本工作区')
        return
      }

      const sceneExists = findSceneById(workspaceId, params.sceneId)
      if (!sceneExists) {
        message.warning('该证据引用的场次在当前剧本中不存在')
        return
      }

      // 再点同一张卡 = 取消高亮（仅在 activeEvidenceId 非 null 时；coverage 卡可能不带 ref id）
      if (params.evidenceRefId != null && activeEvidenceId === params.evidenceRefId) {
        clearEvidenceTrace()
        return
      }

      if (traceDisposeRef.current) {
        try { traceDisposeRef.current() } catch { /* ignore */ }
        traceDisposeRef.current = null
      }
      setActiveEvidenceId(params.evidenceRefId)

      await openFile(params.sceneId, false, true)
      const givenStart = params.startLine ?? null
      const givenEnd = params.endLine ?? null
      const quote = (params.quote || '').trim()
      const expectedSceneText = (sceneExists.text || '').replace(/\r\n/g, '\n')

      for (let attempt = 0; attempt < 12; attempt += 1) {
        const editor = editorRef.current
        const model = editor?.getModel?.()
        const lineCount = Number(model?.getLineCount?.() ?? 0)

        if (model && lineCount > 0) {
          const modelValue = String(model.getValue?.() ?? '').replace(/\r\n/g, '\n')
          const modelMatchesScene = expectedSceneText.length > 0
            ? modelValue === expectedSceneText
            : true
          if (!modelMatchesScene) {
            // eslint-disable-next-line no-await-in-loop
            await new Promise((resolve) => window.setTimeout(resolve, 60))
            continue
          }

          let finalStart: number | null = givenStart
          let finalEnd: number | null = givenEnd ?? givenStart

          if (finalStart == null && quote) {
            const r = findQuoteRangeInText(modelValue, quote)
            if (r) { finalStart = r.start; finalEnd = r.end }
          }

          if (finalStart == null) {
            finalStart = 1
            finalEnd = lineCount
          }

          const start = Math.max(1, Math.min(finalStart, lineCount))
          const end = Math.max(start, Math.min(finalEnd ?? start, lineCount))

          const dispose = highlightLineRange(editor, start, end, { focus: false, ttlMs: 0 })
          traceDisposeRef.current = dispose
          return
        }
        // eslint-disable-next-line no-await-in-loop
        await new Promise((resolve) => window.setTimeout(resolve, 60))
      }
      // eslint-disable-next-line no-console
      console.warn('[ScriptLens] traceEvidence: editor not ready after 12 retries', {
        sceneId: params.sceneId,
      })
    },
    [activeEvidenceId, clearEvidenceTrace, openFile],
  )

  // 切剧本时把上一份溯源高亮 + activeEvidenceId 清掉（避免高亮残留到新剧本上）
  useEffect(() => {
    clearEvidenceTrace()
  }, [snap.workspaceId, clearEvidenceTrace])

  /**
   * 任务派发器：4 类 AgentTask（evidence_lookup / dim_inquiry / fulltext_rewrite / rescore）。
   *
   * - evidence_lookup → 走 traceEvidence（只跳原文，不派 Agent）
   * - dim_inquiry / fulltext_rewrite / rescore → 切 chat tab + 注入 prompt + 可选 autoSubmit
   *
   * options.autoSubmit:
   *   - true  → 不污染 composer，直接走 handleSend({overridePrompt})；
   *             供 RewritePlanCard 点「执行」、AgentDiffReview accept all 后 rescore hook 用
   *   - false（默认）→ 注入 composer 让用户检查 / 追加偏好后手动发
   *
   * 协议详见 docs/03-system-mental-model.md §6 §7。
   */
  const dispatchAgentTask = useCallback(
    async (task: AgentTask, options: { autoSubmit?: boolean } = {}) => {
      const workspaceId = docStudioState.workspaceId
      if (!workspaceId) {
        message.warning('请先选择剧本工作区')
        return
      }

      // evidence_lookup 不再走 dispatch；URL 通道遗留场景兜底转 traceEvidence
      if (task.kind === 'evidence_lookup') {
        await traceEvidence({
          evidenceRefId: task.evidence_ref_id,
          sceneId: task.scene_id,
          startLine: task.start_line ?? null,
          endLine: task.end_line ?? null,
          quote: task.quote ?? null,
        })
        return
      }

      setRightTab('chat')
      const promptText = buildPromptFromTask(task)

      // fe_rescore_hook：fulltext_rewrite execute 派发的同时记下待评分维度，
      // 等 keep 全部 hunk 后由 closeDiffModal 自动派发 rescore（无须再让用户手点）。
      if (task.kind === 'fulltext_rewrite' && task.mode === 'execute') {
        const dims = (task.dimensions || []).filter(Boolean)
        pendingRescoreRef.current = dims.length > 0 ? dims : null
      }

      if (options.autoSubmit) {
        // 不动 composer，直接发；用户在 chat 流里看到自己派出去的简短消息（包含 TASK_META）
        void handleSend({ overridePrompt: promptText, clearComposer: false })
      } else {
        setPrompt(promptText)
      }

      const sceneId = (task as { scene_id?: string }).scene_id
      if (!sceneId) return

      const sceneExists = findSceneById(workspaceId, sceneId)
      if (!sceneExists) {
        message.warning('该证据引用的场次在当前剧本中不存在，已仅派发到 Agent 对话')
        return
      }

      await openFile(sceneId, false, true)
      const startLine = (task as { start_line?: number | null }).start_line ?? null
      const endLine = (task as { end_line?: number | null }).end_line ?? null
      const quote = ((task as { quote?: string | null }).quote || '').trim()
      const expectedSceneText = (sceneExists.text || '').replace(/\r\n/g, '\n')
      if (startLine == null && !quote) return

      // 派 Agent 类用 3s 淡出（视线引导后让用户视线回到 chat composer）
      // 与 traceEvidence 同步采用 quote-first + model 校验，避免 Editor key 重建 race
      for (let attempt = 0; attempt < 12; attempt += 1) {
        const editor = editorRef.current
        const model = editor?.getModel?.()
        const lineCount = Number(model?.getLineCount?.() ?? 0)

        if (model && lineCount > 0) {
          const modelValue = String(model.getValue?.() ?? '').replace(/\r\n/g, '\n')
          const modelMatchesScene = expectedSceneText.length > 0
            ? modelValue === expectedSceneText
            : true

          if (!modelMatchesScene) {
            // eslint-disable-next-line no-await-in-loop
            await new Promise((resolve) => window.setTimeout(resolve, 60))
            continue
          }

          const quoteRange = quote ? findQuoteRangeInText(modelValue, quote) : null
          const finalStart = quoteRange?.start ?? startLine
          const finalEnd = quoteRange?.end ?? endLine ?? finalStart
          if (finalStart != null) {
            highlightLineRange(editor, finalStart, finalEnd ?? finalStart, { focus: false })
          }
          return
        }
        // eslint-disable-next-line no-await-in-loop
        await new Promise((resolve) => window.setTimeout(resolve, 60))
      }
      // eslint-disable-next-line no-console
      console.warn('[ScriptLens] dispatchAgentTask: editor not ready after 12 retries')
    },
    [openFile, setRightTab, setPrompt, traceEvidence],
  )

  // 把最新一份 dispatchAgentTask 注入 ref，供 closeDiffModal 等"先定义后调用"
  // 的回调反向引用，避免 useCallback 循环依赖。
  useEffect(() => {
    dispatchAgentTaskRef.current = dispatchAgentTask
  }, [dispatchAgentTask])


  const loadWorkspaceChatHistory = useCallback(
    async (_workspaceId: string, config: Record<string, any>, sessionIdOverride?: string | null) => {
      const isCurrentWorkspace = () => docStudioState.workspaceId === _workspaceId
      const sessionId = sessionIdOverride ?? config?.session_id ?? config?.sessionId
      if (!sessionId) {
        if (isCurrentWorkspace()) {
          docStudioActions.setChatMessages([])
        }
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
        if (!isCurrentWorkspace()) return
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
        if (isCurrentWorkspace()) {
          docStudioActions.setChatMessages([])
        }
      }
    },
    [showRequestError],
  )

  const isRestoringWorkspaceRef = useRef(false)
  const workspaceListLoadSeqRef = useRef(0)
  const workspaceFilesLoadSeqRef = useRef(0)

  const loadWorkspaceFiles = useCallback(
    async (workspaceId: string, shouldOpenDefault = true) => {
      const loadSeq = ++workspaceFilesLoadSeqRef.current
      const isStaleLoad = () =>
        workspaceFilesLoadSeqRef.current !== loadSeq || docStudioState.workspaceId !== workspaceId
      try {
        isRestoringWorkspaceRef.current = true
        const data = await fetchWorkspaceFiles({ workspaceId }, {
          loading: false,
          errorToast: false,
        })
        if (isStaleLoad()) return
        docStudioActions.setFileTree(data.files)
        docStudioActions.setWorkspaceConfig({
          ...(docStudioState.workspaceConfig || {}),
          ...(data.config || {}),
        })
        applyLlmOptionsFromConfig(data.config)
        // Cursor 逻辑：有 session_id 则加载持久化对话；无则显示「新对话」
        await loadWorkspaceChatHistory(workspaceId, data.config)
        if (isStaleLoad()) return
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
              if (isStaleLoad()) return
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
                  if (isStaleLoad()) return
                }
                // 恢复上次激活的文件
                if (isStaleLoad()) return
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
            if (isStaleLoad()) return
            const firstFile = findFirstFile(data.files)
            if (firstFile) {
              await openFile(firstFile, true)
              if (isStaleLoad()) return
              docStudioActions.setActiveFile(firstFile)
            } else {
              docStudioActions.setActiveFile('')
            }
          }
        }
      } catch (error) {
        if (isStaleLoad()) return
        showRequestError(error)
      } finally {
        if (workspaceFilesLoadSeqRef.current === loadSeq) {
          isRestoringWorkspaceRef.current = false
        }
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
        if (docStudioState.workspaceId !== workspaceId) return
        docStudioActions.setFileTree(data.files)
        // 新上传的剧本第一次拿到 fileTree 时，自动打开第一个场景，让用户立刻看到原文
        // （否则左栏出现场景树但编辑器仍是空白，体验割裂）
        const firstFile = findFirstFile(data.files)
        if (firstFile && !docStudioState.activeFilePath) {
          await openFile(firstFile, true)
          docStudioActions.setActiveFile(firstFile)
        }
      } catch (error) {
        console.warn('[DocStudio] 静默刷新文件树失败', error)
      }
    },
    [openFile],
  )

  // 把 syncWorkspaceFileTree 同步到 ref，让上文中"声明顺序在前"的 callback
  // （如 handleScriptDetailLoaded）可以无依赖地间接调用最新的 sync 函数
  useEffect(() => {
    syncFileTreeRef.current = syncWorkspaceFileTree
  }, [syncWorkspaceFileTree])

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
      .replace(/@scene\d+/gi, ' ')
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
        docStudioActions.setWorkspaceConfig({
          ...(docStudioState.workspaceConfig || {}),
          ...(data.config || {}),
        })
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
      const loadSeq = ++workspaceListLoadSeqRef.current
      docStudioActions.setWorkspaceLoading(true)
      try {
        const list = await listWorkspaces({
          loading: false,
          errorToast: false,
        })
        if (workspaceListLoadSeqRef.current !== loadSeq) return
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
          if (workspaceListLoadSeqRef.current !== loadSeq) return
        } else {
          // 没有任何 Doc Studio 工作区时，明确清空状态，进入 Doc Studio 空白态，
          // 避免从 /doc-studio/notebook 切回 /doc-studio 时残留 Notebook 的文件树和会话。
          docStudioActions.setWorkspaceId('')
        }
      } catch (error) {
        if (workspaceListLoadSeqRef.current !== loadSeq) return
        showRequestError(error)
      } finally {
        if (workspaceListLoadSeqRef.current === loadSeq) {
          docStudioActions.setWorkspaceLoading(false)
        }
      }
    },
    [loadWorkspaceFiles, params.workspaceId],
  )

  useEffect(() => {
    loadWorkspaces(params.workspaceId)
  }, [loadWorkspaces, params.workspaceId])

  useEffect(() => {
    const workspaceId = snap.workspaceId
    if (!isScriptLensSession || !workspaceId) {
      setActiveScriptDetail(null)
      setScriptStatusPolling(false)
      setScriptProcessingStartedAt(null)
      return
    }

    let cancelled = false
    let timer: number | undefined

    const pollScriptStatus = async () => {
      setScriptStatusPolling(true)
      try {
        const detail = await fetchWorkspace(
          { workspaceId },
          { loading: false, errorToast: false },
        )
        if (cancelled || docStudioState.workspaceId !== workspaceId) return

        setActiveScriptDetail(detail)
        docStudioActions.setWorkspaceConfig({
          ...(docStudioState.workspaceConfig || {}),
          ...(detail.config || {}),
          title: detail.name,
        })

        const nextStatus = String(detail.config?.status || '').trim()
        if (SCRIPT_PROCESSING_STATUS.has(nextStatus)) {
          setScriptProcessingStartedAt((prev) => prev ?? Date.now())
          setScriptElapsedNow(Date.now())
          timer = window.setTimeout(
            pollScriptStatus,
            nextStatus === 'pending' ? 2000 : 3000,
          )
          return
        }

        setScriptProcessingStartedAt(null)
        if (nextStatus === 'ready' && !scriptReadyRefreshRef.current[workspaceId]) {
          scriptReadyRefreshRef.current[workspaceId] = true
          // 不再用 `fileTree.length` 防御去重——scriptReadyRefreshRef 已经按 workspaceId
          // 做了"每个剧本只刷一次"的标记，再叠 fileTree.length 反而会把"上一个剧本的
          // 残留 fileTree"误判为"新剧本已加载"，导致新剧本场景树永远不出现（v3.4 修）。
          await loadWorkspaceFiles(workspaceId)
          if (!cancelled && docStudioState.workspaceId === workspaceId) {
            message.success('剧本解析完成，已自动刷新大纲')
          }
        }
      } catch (error) {
        if (!cancelled) {
          console.warn('[ScriptLens] 轮询剧本解析状态失败', error)
        }
      } finally {
        if (!cancelled) setScriptStatusPolling(false)
      }
    }

    void pollScriptStatus()
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [isScriptLensSession, loadWorkspaceFiles, snap.workspaceId])

  useEffect(() => {
    loadKnowledgeBases()
  }, [loadKnowledgeBases])

  useEffect(() => {
    // 不支持编译时（如纯文本工作区）从 compile 退回 chat
    if (!supportsCompilePanel && rightTab === 'compile') {
      setRightTab('chat')
    }
    // ScriptLens 场景下「编译」入口被替换为「分析报告」，自动切走避免空白页
    if (isScriptLensSession && rightTab === 'compile') {
      setRightTab('report')
    }
    // 非 ScriptLens 场景误进 'report'（之前持久化的状态），切回 chat
    if (!isScriptLensSession && rightTab === 'report') {
      setRightTab('chat')
    }
  }, [supportsCompilePanel, rightTab, isScriptLensSession])

  useEffect(() => {
    const workspaceId = snap.workspaceId
    if (!isScriptLensSession || !workspaceId) return
    if (!SCRIPT_PROCESSING_STATUS.has(scriptStatus)) return
    if (autoOpenedReportForScriptRef.current[workspaceId]) return

    autoOpenedReportForScriptRef.current[workspaceId] = true
    setRightPanelClosed(false)
    setRightTab('report')
  }, [isScriptLensSession, scriptStatus, snap.workspaceId])

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
    liveAgentPreviewTextRef.current = liveAgentPreviewText
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

  const handleDeleteCurrentWorkspace = async () => {
    const workspaceId = String(snap.workspaceId || '').trim()
    if (!workspaceId) return
    const fallbackWorkspaceId =
      docStudioWorkspaces.find((item) => item.workspaceId !== workspaceId)?.workspaceId || ''

    setWorkspaceDeleting(true)
    try {
      const deleted = await deleteWorkspace(
        { workspaceId },
        { loading: false, errorToast: false },
      )
      try {
        localStorage.removeItem(`latex_editor_workspace_state_${workspaceId}`)
      } catch (error) {
        console.warn('清理剧本本地打开状态失败', error)
      }
      delete scriptReadyRefreshRef.current[workspaceId]
      setActiveScriptDetail(null)
      setScriptProcessingStartedAt(null)
      setScriptElapsedNow(Date.now())
      message.success(`已删除《${activeWorkspaceName}》及其分析报告、对话状态和时间线`)
      if (!deleted.storage_deleted) {
        message.warning('数据库数据已删除，但原始上传文件此前已不存在或未能删除，请检查后端日志')
      }

      if (fallbackWorkspaceId) {
        navigate(`/doc-studio/${fallbackWorkspaceId}`)
        await loadWorkspaces(fallbackWorkspaceId)
      } else {
        navigate('/doc-studio')
        docStudioActions.setWorkspaces([])
        docStudioActions.setWorkspaceId('')
      }
    } catch (error) {
      message.error(getErrorMessage(error))
    } finally {
      setWorkspaceDeleting(false)
    }
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
      setActiveScriptDetail(workspace)
      setScriptProcessingStartedAt(Date.now())
      setScriptElapsedNow(Date.now())
      delete scriptReadyRefreshRef.current[workspace.workspaceId]
      // ⚠️ 必须清掉上一个剧本的 fileTree / openedFiles 残留——否则 pollScriptStatus 在
      // status=ready 时会被「fileTree 已有内容」误判跳过 loadWorkspaceFiles，导致左栏
      // 卡在旧场景树（视觉上 path 失配 → 显示"无场次"）。
      docStudioActions.setFileTree([])
      docStudioActions.setOpenedFiles([])
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

  /**
   * F2：导出完整剧本（应用全部 script_operations 后的最新版本）。
   * 后端 GET /api/scripts/{id}/export?format=docx|pdf|txt 返回二进制文件。
   */
  const handleExportFullScript = useCallback(async () => {
    if (!snap.workspaceId) {
      message.warning('请先选择剧本')
      return
    }
    setExporting(true)
    try {
      await exportFullScript(snap.workspaceId, exportFormat)
      message.success(`已开始下载 ${exportFormat.toUpperCase()} 文件`)
      setExportModalOpen(false)
    } catch (err: unknown) {
      const e = err as { response?: { status?: number }; message?: string }
      const status = e?.response?.status
      const reason = e?.message || '未知错误'
      message.error(
        status === 404
          ? `后端尚未实现 /export 接口（${reason}）`
          : `导出失败：${reason}`,
      )
    } finally {
      setExporting(false)
    }
  }, [exportFormat, snap.workspaceId])

  const handleCompile = async () => {
    if (!snap.workspaceId) {
      message.warning('请先选择剧本')
      return
    }
    // ScriptLens 场景：编译动作 = 重新诊断（重跑 5 维评分），优先级最高
    if (isScriptLensSession) {
      try {
        await reanalyzeScript(snap.workspaceId)
        message.success('已触发重新诊断，请稍候在「分析报告」中查看最新结果')
      } catch (error) {
        const reason = (error as { message?: string })?.message || String(error)
        message.error(`重新诊断失败：${reason}`)
      }
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
      message.error(getOperationErrorMessage(error, '回滚失败'))
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
      message.error(getOperationErrorMessage(error, '加载历史失败'))
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
      message.error(getOperationErrorMessage(error, '回滚失败'))
    } finally {
      setRevertingOperationId(null)
    }
  }

  const closeDiffModal = useCallback(
    (pathsToRefresh?: string[], contentByPath?: Record<string, string>) => {
      const paths = pathsToRefresh && pathsToRefresh.length > 0 ? pathsToRefresh : []
      paths.forEach((p) => {
        const aliases = snap.workspaceId ? resolveScenePathAliases(snap.workspaceId, p) : []
        const targetPaths = aliases.length ? aliases : [p]
        const content = contentByPath?.[p]
        if (typeof content === 'string') {
          targetPaths.forEach((targetPath) => {
            docStudioActions.setFileContent(targetPath, content)
          })
        } else {
          const preferredPath =
            snap.activeFilePath && targetPaths.includes(snap.activeFilePath)
              ? snap.activeFilePath
              : targetPaths[0]
          void openFile(preferredPath, true)
        }
      })

      // fe_rescore_hook：keep 路径会带 contentByPath（reject 路径不带）；
      // 全部 keep 完毕（最后一个 hunk close）才触发 rescore，避免在 reject 时误派发。
      const isKeepPath = !!contentByPath && Object.keys(contentByPath).length > 0
      const pendingDims = pendingRescoreRef.current
      if (isKeepPath && pendingDims && pendingDims.length > 0) {
        pendingRescoreRef.current = null
        // 微延迟一帧，确保 setFileContent / closeDiffModal 同步状态都落地后再 send
        window.setTimeout(() => {
          void dispatchAgentTaskRef.current?.(
            {
              kind: 'rescore',
              dimensions: pendingDims,
            },
            { autoSubmit: true },
          )
        }, 0)
      } else if (!isKeepPath) {
        // reject 路径：不重评，但要清掉 pending，避免下次 keep 误触发。
        pendingRescoreRef.current = null
      }
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
    [openFile, snap.activeFilePath, snap.workspaceId],
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
    if (diffReverting) return
    setDiffReverting(true)
    try {
      if (!diffOperationId || !diffOperationId.startsWith('db:')) {
        const originalContent = target.original_content ?? ''
        await updateFileContent(
          {
            workspaceId: snap.workspaceId,
            path: target.file_path,
            content: originalContent,
          },
          {
            loading: false,
            errorToast: false,
          },
        )
        if (snap.openedFiles.includes(target.file_path)) {
          await openFile(target.file_path, true, true)
        }
        message.success('已回滚 1 个变更')
      } else {
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
      }

      const nextDiffs = allFileDiffs.filter((_, index) => index !== currentDiffIndex)
      if (!nextDiffs.length) {
        closeDiffModal()
      } else {
        setAllFileDiffs(nextDiffs)
        setCurrentDiffIndex(Math.min(currentDiffIndex, nextDiffs.length - 1))
      }
    } catch (error) {
      message.error(getOperationErrorMessage(error, '回滚失败'))
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

      const contentByPath =
        typeof content === 'string' && currentPath ? { [currentPath]: content } : undefined
      const nextDiffs = allFileDiffs.filter((_, index) => index !== currentDiffIndex)
      if (!nextDiffs.length) {
        closeDiffModal(paths, contentByPath)
        void loadOperationHistory()
      } else {
        if (contentByPath && currentPath) {
          docStudioActions.setFileContent(currentPath, content)
        }
        setAllFileDiffs(nextDiffs)
        setCurrentDiffIndex(Math.min(currentDiffIndex, nextDiffs.length - 1))
      }
    },
    [allFileDiffs, currentDiffIndex, closeDiffModal, loadOperationHistory, resolvedModified],
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
    void loadOperationHistory()
    message.success('已保留全部文件变更')
  }, [allFileDiffs, closeDiffModal, currentDiffIndex, loadOperationHistory, resolvedModified])

  const handleRejectAllDiffs = async () => {
    if (!snap.workspaceId) {
      message.warning('请先选择工作区')
      return
    }
    if (!diffOperationId) {
      message.error('缺少变更 ID（应为 db:<uuid> 或 history:<id>）')
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
      message.error(getOperationErrorMessage(error, '回滚失败'))
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
    liveAgentPreviewTextRef.current = ''
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

      // 按 data shape 自动提取 rewrite_plan：
      // 兼容 propose_full_script_plan_tool / 旧 propose_dimension_rewrite_tool。
      let rewritePlan: RewritePlanData | null = null
      const history = response.execution_history || []
      for (let i = history.length - 1; i >= 0; i -= 1) {
        const step = history[i] as any
        const planRaw =
          step?.result?.data?.rewrite_plan
          ?? step?.result?.rewrite_plan
          ?? null
        if (planRaw && Array.isArray(planRaw.steps)) {
          rewritePlan = planRaw as RewritePlanData
          break
        }
      }

      // Bug 5 兜底：finish-promote 路径已经把 livePreview 当作 reply 推进 chat 流，
      // 这里跳过重复 push（但仍执行下方 file_diffs / executionHistory / operationId 等副作用）。
      // 若有 rewritePlan，需要补 push 一条带 plan 的消息以便 RewritePlanCard 渲染。
      if (livePromotedToChatRef.current && !rewritePlan) {
        livePromotedToChatRef.current = false
      } else {
        livePromotedToChatRef.current = false
        pushChatMessage({
          role: 'agent',
          content: response.execution_history?.[response.execution_history.length - 1]?.content
            ? response.execution_history[response.execution_history.length - 1].content
            : `已执行变更 ${changeCount} 项`,
          meta: {
            changes: response.changes,
            traceId: response.trace_id || traceId,
            operationId,
            rewritePlan,
          },
        })
      }
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
    
    const promptForAgent = linkedSelections.length > 0
      ? injectSelectionBlocksIntoPrompt(finalPrompt, linkedSelections)
      : finalPrompt
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
    livePromotedToChatRef.current = false
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
            userIntent: promptForAgent,
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
              text = `任务开始 · Op ${formatOperationIdForDisplay(opId)}`
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
            if (BLOCKED_RUNTIME_LLM_MODELS.has(actualModel.toLowerCase())) {
              message.error(`运行时拒绝切换到弱化模型：${actualModel}`)
              return
            }
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
            // Bug 5 兜底（被 LLM 拒答 / 后端 finalize 卡住时 result 不来 spinner 永转）：
            // finish 之后 reply 文本已经在 livePreview 流完，
            // 5s 内 result 没到就把 livePreview 转成 chat 消息 + 关闭 spinner，
            // 让用户立刻能继续提问；保留 SSE，result 真到达时 handleAgentResponse
            // 通过 livePromotedToChatRef 跳过重复 pushChatMessage，但仍处理 file_diffs / op 等副作用。
            window.setTimeout(() => {
              if (asyncRunResolvedRef.current) return
              if (livePromotedToChatRef.current) return
              const partial = (liveAgentPreviewTextRef.current || '').trim()
              if (!partial) return
              livePromotedToChatRef.current = true
              pushChatMessage({
                role: 'agent',
                content: partial,
                meta: { traceId, partial: true, source: 'live_preview_promoted' },
              })
              resetLiveAgentPreview()
              activeRunIdRef.current = null
              pendingSendRef.current = null
              setChatLoading(false)
            }, 5000)
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
                if (BLOCKED_RUNTIME_LLM_MODELS.has(actualModel.toLowerCase())) {
                  message.error(`运行时拒绝切换到弱化模型：${actualModel}`)
                } else {
                  setLlmModel(actualModel)
                  const requestedModel = String(runtimeModel.requested_model || llmModel || '').trim()
                  const fromLabel = requestedModel ? resolveRuntimeModelLabel(requestedModel) : '所选模型'
                  const toLabel = resolveRuntimeModelLabel(actualModel)
                  message.warning(`所选模型不可用，已自动切换为真实使用模型：${fromLabel} → ${toLabel}`)
                }
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
          userIntent: promptForAgent,
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
    const items: MenuProps['items'] = []
    if (isScriptLensSession) {
      // F2：剧本场景下，原「下载当前场次」改为「导出完整剧本」（docx / pdf / txt）
      // 后端已应用所有 script_operations（改写历史），导出的是当前最终版本。
      items.push({
        key: 'export-script',
        label: '导出完整剧本',
        icon: <DownloadOutlined />,
        disabled: !snap.workspaceId,
      })
    } else {
      items.push({
        key: 'download',
        label: '下载当前文件',
        icon: <DownloadOutlined />,
        disabled: !snap.activeFilePath,
      })
    }
    items.push({
      key: 'delete',
      label: isScriptLensSession ? '从工作区移除当前场次' : '删除当前文件',
      icon: <DeleteOutlined />,
      disabled:
        !snap.activeFilePath || snap.activeFilePath === FULL_SCRIPT_VIRTUAL_PATH,
      danger: true,
    })
    if (lastOperationId && !undoingLastApply) {
      items.push({
        key: 'undo',
        label: '撤销最近一次改写',
        icon: <SyncOutlined />,
      })
    }
    items.push({
      key: 'history',
      label: isScriptLensSession ? '改写历史时间线' : '文件时间线',
      icon: <HistoryOutlined />,
    })
    return items
  }, [
    isScriptLensSession,
    lastOperationId,
    snap.activeFilePath,
    snap.workspaceId,
    undoingLastApply,
  ])

  const handleHeaderOverflowMenuClick = useCallback<NonNullable<MenuProps['onClick']>>(
    ({ key }) => {
      if (key === 'download') {
        void handleDownloadCurrentFile()
        return
      }
      if (key === 'export-script') {
        setExportFormat('docx')
        setExportModalOpen(true)
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
      // D1：完整剧本虚拟节点没有真实文件，禁用 hover actions（重命名 / 删除）
      const isVirtualFullScript = node.path === FULL_SCRIPT_VIRTUAL_PATH
      const isProtectedDirectory =
        node.type === 'directory' && isNotebookSystemPath(node.path, { protectParents: true })
      const isProtectedFile =
        isVirtualFullScript || (node.type === 'file' && isNotebookSystemPath(node.path))
      return (
        <span
          className="doc-studio__tree-node"
          onMouseEnter={() => setHoveredTreePath(node.path)}
          onMouseLeave={() => {
            setHoveredTreePath((prev) => (prev === node.path ? '' : prev))
          }}
        >
          <span
            className={`doc-studio__tree-node-main ${
              isScriptLensSession
                ? node.type === 'directory'
                  ? 'doc-studio__tree-node-main--episode'
                  : isVirtualFullScript
                    ? 'doc-studio__tree-node-main--full'
                    : 'doc-studio__tree-node-main--scene'
                : ''
            }`}
          >
            {/* ScriptLens 场景下用大纲样式（集 = 加粗章节、场 = 缩进项目符号），
                不再像文件管理器那样塞文件夹/文件图标——这本质上是剧本目录，不是 FS。 */}
            {isScriptLensSession ? (
              isVirtualFullScript ? null : node.type === 'directory' ? (
                <span className="doc-studio__outline-mark doc-studio__outline-mark--episode" aria-hidden />
              ) : (
                <span className="doc-studio__outline-mark doc-studio__outline-mark--scene" aria-hidden />
              )
            ) : node.type === 'directory' ? (
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
              {!isVirtualFullScript && (
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
              )}
            </span>
          )}
        </span>
      )
    },
    [hoveredTreePath, isNotebookSystemPath, openRenameModal, showDeleteConfirm, isScriptLensSession],
  )

  // D1：左栏展示层注入「📖 完整剧本」虚拟顶节点；不动 snap.fileTree，
  // 让其他依赖原始树的逻辑（lookupName / collectAllFilePaths / findNode）保持行为不变。
  const displayFileTree = useMemo(
    () => wrapWithFullScriptNode((snap.fileTree || []) as DocStudioAPI.FileNode[]),
    [snap.fileTree],
  )

  const treeData = useMemo(
    () => buildTreeData(cloneFileNodes(displayFileTree), renderTreeNodeTitle),
    [renderTreeNodeTitle, displayFileTree],
  )

  const isAgentDiffReviewActive =
    agentDiffReviewOpen && diffModalContext === 'agent' && allFileDiffs.length > 0
  const hasPendingAgentReview =
    diffModalContext === 'agent' && allFileDiffs.length > 0
  const currentReviewDiff = isAgentDiffReviewActive ? allFileDiffs[currentDiffIndex] : undefined

  const renderScriptStatusPanel = () => {
    const label = SCRIPT_STATUS_LABELS[scriptStatus] || '准备中'
    const totalEpisodes = Number(
      activeScriptDetail?.config?.total_episodes ||
      snap.workspaceConfig?.total_episodes ||
      0,
    )
    const inferredEpisodes = inferEpisodeUpperBoundFromTitle(activeWorkspaceName)
    const totalScenes = Number(
      activeScriptDetail?.config?.total_scenes ||
      snap.workspaceConfig?.total_scenes ||
      0,
    )
    const elapsedText =
      scriptProcessingElapsedSec >= 60
        ? `${Math.floor(scriptProcessingElapsedSec / 60)}分${scriptProcessingElapsedSec % 60}秒`
        : `${scriptProcessingElapsedSec}秒`
    const failureReason = String(
      activeScriptDetail?.config?.failure_reason ||
      snap.workspaceConfig?.failure_reason ||
      '',
    ).trim()

    if (isScriptFailed) {
      return (
        <div className="doc-studio__script-status-panel doc-studio__script-status-panel--failed">
          <div className="doc-studio__script-status-card">
            <Tag color="error">解析失败</Tag>
            <h2>剧本解析失败</h2>
            <p className="doc-studio__script-status-desc">
              {failureReason || '后台解析未返回具体原因，请查看服务日志后重新上传。'}
            </p>
            <Space>
              <Button onClick={() => snap.workspaceId && loadWorkspaces(snap.workspaceId)}>
                刷新状态
              </Button>
              <Button type="primary" onClick={() => setWorkspaceModalOpen(true)}>
                重新上传剧本
              </Button>
            </Space>
          </div>
        </div>
      )
    }

    return (
      <div className="doc-studio__script-status-panel">
        <div className="doc-studio__script-status-card">
          <Tag color="pink">ScriptLens 正在准备工作区</Tag>
          <h2>正在解析《{activeWorkspaceName}》</h2>
          <p className="doc-studio__script-status-desc">
            系统正在读取全文、切分集场并写入检索索引。完成后会自动刷新左侧大纲并打开正文。
          </p>
          <Progress
            percent={scriptProcessingProgress}
            status="active"
            strokeColor="#E07A8C"
            trailColor="#F7E9E5"
          />
          <div className="doc-studio__script-status-steps">
            {['上传完成', '文本解析', '集场切分', '检索索引', '进入编辑'].map((item, index) => (
              <span
                key={item}
                className={index * 25 <= scriptProcessingProgress ? 'is-active' : ''}
              >
                {item}
              </span>
            ))}
          </div>
          <div className="doc-studio__script-status-meta">
            <span>当前阶段：{label}</span>
            <span>已等待：{elapsedText}</span>
            {totalEpisodes > 0 && <span>{totalEpisodes} 集</span>}
            {!totalEpisodes && inferredEpisodes > 0 && <span>约 {inferredEpisodes} 集</span>}
            {totalScenes > 0 && <span>{totalScenes} 场</span>}
            {scriptStatusPolling && <span>正在刷新状态…</span>}
          </div>
          <Alert
            type="info"
            showIcon
            message={
              scriptProcessingIsLarge
                ? '这是一份较大的短剧剧本，解析和检索索引可能需要更久。请耐心等待，不要重复上传同一文件。'
                : '请稍等片刻，不需要刷新页面；解析完成后会自动进入剧本大纲。'
            }
          />
          <Space>
            <Button onClick={() => snap.workspaceId && loadWorkspaces(snap.workspaceId)}>
              手动刷新状态
            </Button>
            <Button type="text" onClick={() => setWorkspaceModalOpen(true)}>
              上传另一份剧本
            </Button>
          </Space>
        </div>
      </div>
    )
  }

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
                {isScriptLensSession && (
                  <Popconfirm
                    title="删除当前剧本？"
                    description="会同时删除分析报告、证据引用、改写时间线和本地打开状态，删除后不可恢复。"
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true, loading: workspaceDeleting }}
                    onConfirm={handleDeleteCurrentWorkspace}
                  >
                    <Tooltip title={snap.workspaceId ? '删除当前剧本' : '请先选择剧本'}>
                      <Button
                        danger
                        icon={<DeleteOutlined />}
                        size="small"
                        disabled={!snap.workspaceId || workspaceDeleting}
                        loading={workspaceDeleting}
                      />
                    </Tooltip>
                  </Popconfirm>
                )}
                {/* 整剧分析入口已统一收敛到右栏「分析报告」面板（report-first）；
                    上传后自动跑、右栏自动展开，不再开新网页。 */}
              </Space>
            </div>
            <div className="doc-studio__explorer-header" onContextMenu={handleExplorerContextMenu}>
              <span className="doc-studio__explorer-name" title={activeWorkspaceName}>
                {/* 顶部 Select 已经显示当前剧本名，这里只标示"这一栏的内容是什么"——
                    剧本大纲（集 → 场层级），跟 markdown TOC 同义。 */}
                {isScriptLensSession ? '剧本大纲' : explorerTitle}
              </span>
              <div className="doc-studio__explorer-actions">
                {/* R3：ScriptLens 场景下隐藏「新建文件 / 新建目录 / 上传文件」3 个按钮——
                    场次由后端按集场切分而来，用户没有「手动新建一场」或「往剧本里再塞一个文件」
                    这种语义。新增剧本走顶部「+」上传 Modal。 */}
                {!isScriptLensSession && (
                  <>
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
                  </>
                )}
                <Tooltip title={isScriptLensSession ? '刷新场次列表' : '刷新文件树'}>
                  <Button
                    type="text"
                    className="doc-studio__explorer-action-btn"
                    icon={<ReloadOutlined />}
                    disabled={!snap.workspaceId}
                    onClick={() => refreshFileTree(true)}
                  />
                </Tooltip>
                <Tooltip title={isScriptLensSession ? '折叠场次栏 (Ctrl+B)' : '折叠文件栏 (Ctrl+B)'}>
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
            <div
              className={`doc-studio__tree-wrapper ${isScriptLensSession ? 'doc-studio__tree-wrapper--outline' : ''}`}
              onContextMenu={handleExplorerContextMenu}
            >
              {treeData.length ? (
                <Tree
                  selectedKeys={snap.activeFilePath ? [snap.activeFilePath] : []}
                  expandedKeys={expandedKeys}
                  onExpand={(keys) => setExpandedKeys(keys)}
                  treeData={treeData}
                  onSelect={handleTreeSelect}
                  onRightClick={handleRightClick}
                />
              ) : isScriptProcessing ? (
                <div className="doc-studio__outline-loading">
                  <Spin size="small" />
                  <div>
                    <div className="doc-studio__outline-loading-title">
                      正在生成剧本大纲
                    </div>
                    <div className="doc-studio__outline-loading-desc">
                      {SCRIPT_STATUS_LABELS[scriptStatus] || '解析中'}，完成后自动出现集场目录
                    </div>
                  </div>
                  <div className="doc-studio__outline-loading-bars" aria-hidden>
                    <span />
                    <span />
                    <span />
                  </div>
                </div>
              ) : (
                <Empty
                  description={
                    isScriptLensSession
                      ? snap.workspaceId
                        ? '该剧本暂无场次（解析中或解析失败）'
                        : '请先在顶部「+」上传一份剧本'
                      : '暂无文件'
                  }
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
                    {supportsCompilePanel && !isScriptLensSession && (
                      // ScriptLens 场景刻意不展示这个按钮：
                      //   1. 它原本是 LaTeX/Markdown 的"编译"动作，剧本场景没有"编译当前场"语义；
                      //   2. 把它复用为"重跑全剧 5 维评分"会让用户误以为是"分析当前场"——位置在
                      //      编辑器顶部、又是 ▶️ 图标，左侧又开着具体一场，混淆面太大；
                      //   3. 整剧分析的入口已经统一为：上传后自动跑 + 右栏「分析报告」面板内的
                      //      「重新诊断」按钮（覆盖旧报告）。
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
                          theme={SCRIPTLENS_LIGHT_THEME}
                          height="100%"
                          language={resolveEditorLanguage(snap.activeFilePath)}
                          loading={<Spin />}
                          value={currentFileBuffer?.content || ''}
                          onChange={handleEditorChange}
                          onMount={handleEditorMount}
                          options={{
                            // D1：完整剧本视图是虚拟拼接，不能写回任何 scene，强制只读
                            readOnly:
                              currentFileBuffer?.loading ||
                              snap.activeFilePath === FULL_SCRIPT_VIRTUAL_PATH,
                            minimap: { enabled: false },
                            // 剧本场景：14 在浅色背景下偏紧凑，15 + 行高 24 阅读最舒服
                            fontSize: 15,
                            lineHeight: 24,
                            letterSpacing: 0.3,
                            fontFamily:
                              '"JetBrains Mono", "Cascadia Code", "SF Mono", Menlo, Consolas, "Sarasa Mono SC", "Microsoft YaHei UI", "PingFang SC", monospace',
                            wordWrap: 'on',
                            automaticLayout: true,
                            selectOnLineNumbers: true,
                            scrollBeyondLastLine: false,
                            renderLineHighlight: 'gutter',
                            smoothScrolling: true,
                            cursorBlinking: 'smooth',
                            padding: { top: 16, bottom: 16 },
                            // ???????????????Ctrl+A, Ctrl+C, Ctrl+V, Ctrl+Z ??
                            // Monaco Editor ?????????????????
                          }}
                        />
                      ) : (
                        snap.workspaceLoading ? null : (
                          isScriptProcessing || isScriptFailed
                            ? renderScriptStatusPanel()
                            : <DocStudioWelcome onUploadClick={() => setWorkspaceModalOpen(true)} />
                        )
                      )}
                    </div>
                </div>
              ) : (
                snap.workspaceLoading ? null : (
                  isScriptProcessing || isScriptFailed
                    ? renderScriptStatusPanel()
                    : <DocStudioWelcome onUploadClick={() => setWorkspaceModalOpen(true)} />
                )
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
                {/* ScriptLens 专属：分析报告摘要 tab，PRD §5 「report-first」入口 */}
                {isScriptLensSession && (
                  <button
                    className={`doc-studio__custom-tab ${rightTab === 'report' ? 'doc-studio__custom-tab--active' : ''}`}
                    onClick={() => setRightTab('report')}
                  >
                    分析报告
                  </button>
                )}
                <button
                  className={`doc-studio__custom-tab ${rightTab === 'history' ? 'doc-studio__custom-tab--active' : ''}`}
                  onClick={() => setRightTab('history')}
                >
                  时间线
                </button>
                {supportsCompilePanel && !isScriptLensSession && (
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
                          {/* 「新对话」按钮已删除：右上角「+」就是新建语义，再放一个 tab 是重复入口。 */}
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
                                  {msg.meta?.rewritePlan ? (
                                    <RewritePlanCard
                                      plan={msg.meta.rewritePlan as RewritePlanData}
                                      executed={!!msg.meta?.rewritePlanExecuted}
                                      onTraceScene={(sceneId) => {
                                        void openFile(sceneId, false, true)
                                      }}
                                      onDispatchExecute={(task) => {
                                        // 与 RewritePlanCard 契约：父级负责 autoSubmit + 标记 executed，
                                        // 防止用户在同一计划卡上重复点击。
                                        docStudioActions.updateMessageMeta(msg.id, {
                                          rewritePlanExecuted: true,
                                        })
                                        void dispatchAgentTask(task, { autoSubmit: true })
                                      }}
                                    />
                                  ) : null}
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
                      {/* R1（PRD §8 对齐）：原「AI 改写本场」按钮已移除。
                          改写按 target_dimension 驱动而非 scene_id 驱动，入口收口到：
                          (a) 报告页低分维度卡片「获取改写建议」按钮
                          (b) chat 输入框上方「针对 X 维度给改写建议」快捷指令
                          chat 内调用 propose_rewrite_tool 的能力保留，由 Agent 自然触发。 */}
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
                        {/* F1(b)：剧本场景下，输入框上方的「5 维改写」快捷指令。
                            点一下预填到 composer，让用户可继续追加细节再发送。 */}
                        {isScriptLensSession && snap.workspaceId ? (
                          <div className="doc-studio__rewrite-quick-row">
                            <Space size={6} wrap>
                              <span className="doc-studio__rewrite-quick-label">改写建议：</span>
                              {Object.entries(REWRITE_QUICK_COMMANDS).map(([dim, cmd]) => (
                                <Tooltip key={dim} title={cmd.prompt}>
                                  <Button
                                    size="small"
                                    type="default"
                                    onClick={() => fillRewritePromptByDimension(dim)}
                                  >
                                    {cmd.label}
                                  </Button>
                                </Tooltip>
                              ))}
                            </Space>
                          </div>
                        ) : null}
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
                                : isScriptLensSession
                                  ? '针对当前场提问 / 改写（整剧 5 维分析请用左上「整剧分析」）—— Enter 发送，Shift+Enter 换行'
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
                {/* ScriptLens Report Rail：右栏分析报告（report-first，所有内容直接展示） */}
                {isScriptLensSession && rightTab === 'report' && (
                  snap.workspaceId ? (
                    <ScriptlensReportRail
                      scriptId={snap.workspaceId}
                      activeEvidenceId={activeEvidenceId}
                      onTraceEvidence={traceEvidence}
                      onClearTrace={clearEvidenceTrace}
                      onDispatchTask={(task) => dispatchAgentTask(task, { autoSubmit: true })}
                      onScriptDetailLoaded={handleScriptDetailLoaded}
                    />
                  ) : (
                    <div style={{ padding: 16 }}>
                      <Empty
                        description="请先在左侧选择剧本"
                        image={Empty.PRESENTED_IMAGE_SIMPLE}
                      />
                    </div>
                  )
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
                                <Text type="secondary">
                                  Op: {formatOperationIdForDisplay(item.operation_id)}
                                </Text>
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
                                <Tooltip title="点击后会把当前文件回退到该时间点的 before 快照（snapshot_before）。仅影响所选文件，其他文件不受影响。">
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
        okText="上传"
        okButtonProps={{ disabled: !newWorkspaceFile }}
        onCancel={() => {
          setWorkspaceModalOpen(false)
          setNewWorkspaceFile(null)
          setNewWorkspaceName('')
          setNewWorkspaceType('latex')
        }}
        confirmLoading={workspaceSubmitting}
      >
        <Form layout="vertical">
          <Form.Item
            label="剧本文件"
            extra="支持 .docx / .pdf / .txt / .md，单文件 ≤50MB；剧本标题默认用文件名"
          >
            <input
              type="file"
              accept=".docx,.pdf,.txt,.md,.markdown"
              onChange={(event) => {
                const file = event.target.files?.[0] || null
                setNewWorkspaceFile(file)
                setNewWorkspaceName(file ? file.name.replace(/\.[^.]+$/, '') : '')
              }}
            />
          </Form.Item>
        </Form>
      </Modal>
      {/* R1：旧的「AI 改写本场」Modal 已移除——改写按 target_dimension 驱动，
          入口在报告页（低分维度卡）和 chat 快捷指令上。 */}
      {/* F2：导出完整剧本 Modal（应用所有改写历史后的最终版本） */}
      <Modal
        title="导出完整剧本"
        open={exportModalOpen}
        onOk={() => {
          void handleExportFullScript()
        }}
        okText="开始导出"
        confirmLoading={exporting}
        onCancel={() => {
          if (exporting) return
          setExportModalOpen(false)
        }}
        maskClosable={!exporting}
        keyboard={!exporting}
        width={460}
      >
        <Form layout="vertical">
          <Form.Item label="选择格式" extra="导出的文件已应用全部改写历史，是当前最新版本">
            <Radio.Group
              value={exportFormat}
              onChange={(e) => setExportFormat(e.target.value as ScriptExportFormat)}
              optionType="button"
              buttonStyle="solid"
              options={[
                { label: 'DOCX（Word）', value: 'docx' },
                { label: 'PDF', value: 'pdf' },
                { label: 'TXT', value: 'txt' },
              ]}
            />
          </Form.Item>
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


