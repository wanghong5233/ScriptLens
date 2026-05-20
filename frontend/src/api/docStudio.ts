/**
 * docStudio.ts —— ScriptLens 适配层
 *
 * 这一层保持原 doc-studio 的导出函数签名不变（doc-studio/index.tsx 8000 行
 * 的 import 不动），但内部实现统一接 ScriptLens 后端的 11 个 `/api/scripts/*`
 * 端点。
 *
 * 对齐策略（参考 PRD 业务模型）：
 *   - workspace ↔ script  (script_id 直接当 workspace_id 用)
 *   - file      ↔ scene   (path = scene_id；virtual 目录 path = `__ep_<n>`)
 *   - 文件树   ↔ scenes 按 episode 分组
 *
 * 不能对齐的功能（场不可变 / 无编译 / 无 operations / 无 PDF 等）一律 fail
 * aloud：直接抛 `Error('ScriptLens 不支持: ...')`，由 UI 上层弹 toast。
 *
 * chat 链路（EventSource GET 协议）与 ScriptLens 的 `POST /chat → SSE`
 * 通过 `sseClient` 做桥接：runAgentTaskAsync 保存请求快照，events 阶段再发起
 * 实际 fetch+ReadableStream，并把后端事件转发给 doc-studio/index.tsx。
 */

import { AxiosRequestConfig } from 'axios'
import { getApiBase } from './env'
import { request } from './request'
import {
  ScriptLensAgentStream,
  openScriptLensAgentStream,
  rememberChatArgs,
} from './sseClient'

const API_BASE = getApiBase()
const SCRIPTS_BASE = `${API_BASE}/scripts`

// ============================================================
// ScriptLens 后端响应原始 DTO（与 backend/app/schemas/script.py 对齐）
// ============================================================

type ScriptStatus = 'pending' | 'parsing' | 'indexing' | 'ready' | 'failed'

type ScriptListItemDTO = {
  id: string
  title: string
  status: ScriptStatus
  total_episodes?: number | null
  total_scenes?: number | null
  created_at: string
}

type ScriptDetailDTO = {
  id: string
  title: string
  source_format: string
  status: ScriptStatus
  total_episodes?: number | null
  total_scenes?: number | null
  total_chars?: number | null
  failure_reason?: string | null
  created_at: string
  updated_at: string
}

type SceneItemDTO = {
  id: string
  episode_no?: number | null
  scene_no: string
  scene_label: string
  characters: string[]
  text: string
  start_line?: number | null
  end_line?: number | null
}

type ScenesResponseDTO = {
  script_id: string
  total: number
  scenes: SceneItemDTO[]
}

type ScriptUploadResponseDTO = {
  id: string
  title: string
  source_format: string
  status: ScriptStatus
}

// ============================================================
// Report DTO（与 backend/app/schemas/script.py ReportPayload 对齐）
// ============================================================

// 阅文五力（docs/08-evaluation-framework.md §3）；compliance 独立见 ComplianceDTO
export type DimensionKey =
  | 'story'
  | 'character'
  | 'concept'
  | 'emotion'
  | 'pacing'

// 五力评分档位（合规审核 4 档另见 ComplianceLevel）
export type ScoreLevel = 'high' | 'medium' | 'low'

export interface ScorecardItemDTO {
  dimension: DimensionKey
  score: number | null
  level: ScoreLevel | null
  reason: string
  evidence_ref_ids: string[]
}

// 合规审核独立字段（不进 scorecard，不计入 overall_score）
export type ComplianceLevel = 'high_risk' | 'medium_risk' | 'low_risk' | 'clean'

export interface ComplianceDTO {
  dimension: 'compliance'
  score: number | null
  level: ComplianceLevel | null
  reason: string
  evidence_ref_ids: string[]
}

export interface DecisionCardDTO {
  label: string
  confidence: 'high' | 'medium' | 'low'
  one_sentence_reason: string
  summary?: string
}

// 后端 ReportPayload.risk_flags 是 List[str]（category 字符串列表，PRD §7）
export type RiskFlagDTO = string

// 与 backend.ReportEvidenceRef 严格对齐（id / quote / scene_label 真实字段名）
// v3.3 line-range anchored citation：start_line/end_line 是主锚点，quote 仅 tooltip
export interface EvidenceRefDTO {
  id: string
  scene_id: string
  episode_no?: number | null
  scene_no?: string | null
  scene_label?: string | null
  /** 主锚点：scene 内行号区间起点（1-based） */
  start_line?: number | null
  /** 主锚点：scene 内行号区间终点（1-based 闭区间） */
  end_line?: number | null
  /** 该 line_range 对应的原文片段，仅用于 tooltip / preview。前端跳转**不**依赖此字段 */
  quote: string
  /** quote 来源标记：reward:<event_type> / risk_hit / fallback_first_line */
  quote_source?: string | null
  /** 对整场戏的摘要，不是 quote 碎片；用于「关键场景」卡片 */
  scene_summary?: string | null
  reason: string
  confidence?: 'high' | 'medium' | 'low'
}

/**
 * 「主要看点」节点（reward / hook / twist / risk 等剧本关键事件）。
 *
 * 与 backend.ReportPayload.highlights 对齐。前端在报告里渲染成一条条「人话坐标 + 一句话」
 * 列表，点击单条时联动编辑器跳到原文 + 持久高亮（溯源语义，不派 Agent）。
 *
 * highlight_type 设计：
 *   - hook       开场钩子（首集核心冲突）
 *   - face_slap  打脸 / 反转
 *   - reversal   命运反转
 *   - revenge    复仇 / 报应
 *   - cp_progress  CP 进展
 *   - identity_reveal 身份揭露
 *   - villain_fall    反派败落
 *   - underdog_rise   逆袭
 *   - scheme_exposed  阴谋败露
 *   - risk             审核风险点
 */
export type HighlightType =
  | 'hook'
  | 'face_slap'
  | 'reversal'
  | 'revenge'
  | 'cp_progress'
  | 'identity_reveal'
  | 'villain_fall'
  | 'underdog_rise'
  | 'scheme_exposed'
  | 'risk'

// v3.3 line-range anchored：start_line/end_line 是主锚点，evidence 仅 tooltip
export interface HighlightDTO {
  id: string
  type: HighlightType
  scene_id: string
  episode_no?: number | null
  scene_no?: string | null
  scene_label?: string | null
  /** 主锚点：scene 内行号区间起点（1-based） */
  start_line?: number | null
  /** 主锚点：scene 内行号区间终点（1-based 闭区间） */
  end_line?: number | null
  /** 一句话点题（"宁卓 vs 苏怀瑾摊牌"），≤ 40 字 */
  oneliner: string
  /** 原文片段（≤ 80 字），仅 tooltip / 折叠态展示。前端跳转不依赖此字段 */
  evidence?: string | null
}

export type RecommendationDTO = 'recommend' | 'consider' | 'pass'

// v3.3 line-range anchored：跳转锚点 = (anchor_scene_id, evidence_line_range)
// evidence_quote 仅 tooltip / preview 展示，不参与跳转计算
export interface CoveragePointDTO {
  title: string
  detail: string
  anchor_scene_id?: string | null
  /**
   * LLM 写卡片时同次给出的 scene 内行号区间 [start_line, end_line]（1-based 闭区间）。
   * **跳转高亮的主锚点** —— 前端 deltaDecorations 直接高亮这一区间。
   */
  evidence_line_range?: [number, number] | null
  /** evidence_line_range 对应的原文摘要，仅用于 hover tooltip。前端跳转**不**用此字段定位 */
  evidence_quote?: string | null
}

export interface CoverageCardDTO {
  logline: string
  recommendation: RecommendationDTO
  confidence: 'high' | 'medium' | 'low'
  genre: string[]
  core_value: string
  strengths: CoveragePointDTO[]
  concerns: CoveragePointDTO[]
}

export type BeatTypeDTO =
  | 'opening'
  | 'inciting'
  | 'midpoint'
  | 'climax'
  | 'closing'
  | 'twist'
  | 'reward'

export interface BeatNodeDTO {
  type: BeatTypeDTO
  summary: string
  anchor_scene_id: string
}

export interface BeatActDTO {
  act: 1 | 2 | 3
  title: string
  scene_range: string[]
  beats: BeatNodeDTO[]
}

export interface BeatSheetDTO {
  acts: BeatActDTO[]
}

export type CharacterGraphRoleDTO =
  | 'protagonist'
  | 'antagonist'
  | 'support'
  | 'minor'

export type CharacterRelationTypeDTO =
  | 'family'
  | 'romance'
  | 'rival'
  | 'ally'
  | 'authority'
  | 'deception'
  | 'mentor'

export type RelationPolarityDTO = 'positive' | 'negative' | 'mixed'

export interface CharacterGraphNodeDTO {
  id: string
  name: string
  role: CharacterGraphRoleDTO
  motivation: string
  goal: string
  obstacle: string
  first_scene_id?: string | null
  appearance_count: number
}

export interface CharacterGraphEdgeDTO {
  source_id: string
  target_id: string
  type: CharacterRelationTypeDTO
  weight: number
  polarity: RelationPolarityDTO
}

export interface CharacterGraphDTO {
  nodes: CharacterGraphNodeDTO[]
  edges: CharacterGraphEdgeDTO[]
}

export interface PacingCurvePointDTO {
  episode_no: number
  scene_count: number
  event_count: number
  hooks: number
  twists: number
  reward_events: number
  sentiment: number
}

export interface EvaluationDimensionDTO {
  key: DimensionKey
  label: string
  score: number | null
  level: ScoreLevel | null
  reason: string
  evidence_ref_ids: string[]
}

export interface EvaluationPayloadDTO {
  dimensions: EvaluationDimensionDTO[]
  risk_flags: string[]
  rewrite_seeds: unknown[]
}

export interface ReportPayloadDTO {
  script_id: string
  title?: string
  decision: DecisionCardDTO
  decision_reason?: string
  overall_score?: number | null
  summary?: string
  must_read_scene_ids: string[]
  /** 阅文五力评分（docs/08-evaluation-framework.md §3） */
  scorecard: ScorecardItemDTO[]
  /** 合规审核独立字段（docs/08 §4），不进 scorecard 不计入 overall_score */
  compliance?: ComplianceDTO | null
  evidence_refs: EvidenceRefDTO[]
  /** 主要看点 / 钩子 / 反转 / 爽点列表（与 reward_events 同源，前端按 type 分组渲染） */
  highlights?: HighlightDTO[]
  /** v3 速览：logline + recommendation + 优劣点 */
  coverage_card?: CoverageCardDTO | null
  /** v3 故事：三幕骨架 + 关键节拍 */
  beat_sheet?: BeatSheetDTO | null
  /** v3 人物：角色节点 + 关系边 */
  character_graph?: CharacterGraphDTO | null
  /** v3 故事：每集事件密度 + 情感弧 */
  pacing_curve?: PacingCurvePointDTO[]
  /** v3 评估：五力评分 + 改写候选 */
  evaluation?: EvaluationPayloadDTO | null
  risk_flags: RiskFlagDTO[]
  report_id?: string
  generated_at?: string
}

export interface ReportResponseDTO {
  script_id: string
  report: ReportPayloadDTO | null
  generated_at?: string | null
}

export interface ReportNotReadyDTO {
  script_id: string
  status: ScriptStatus
  failure_reason?: string | null
}

export type ReportFetchResult = ReportResponseDTO | ReportNotReadyDTO

export function isReportReady(
  result: ReportFetchResult,
): result is ReportResponseDTO {
  return 'report' in result && result.report !== null && result.report !== undefined
}

// ============================================================
// 内部工具：scene cache（workspaceId → scenes[]）
// ============================================================

const sceneCache = new Map<string, SceneItemDTO[]>()

function unsupported(feature: string): never {
  throw new Error(`ScriptLens 不支持: ${feature}`)
}

function toUnixMs(iso?: string | null): number {
  if (!iso) return 0
  const t = Date.parse(iso)
  return Number.isFinite(t) ? t : 0
}

function withScripts(config?: AxiosRequestConfig): AxiosRequestConfig {
  return {
    baseURL: SCRIPTS_BASE,
    ...config,
    headers: { ...(config?.headers ?? {}) },
  }
}

function sceneFileName(s: SceneItemDTO): string {
  const ep = s.episode_no != null ? `第${s.episode_no}集·` : ''
  const label = s.scene_label ? `《${s.scene_label}》` : ''
  return `${ep}${s.scene_no}场${label ? ` ${label}` : ''}`
}

const VFS_SCENE_PATH_RE = /^scenes\/E(\d{2,})-S(\d{3})\.txt$/i

function parseSceneIndex(sceneNo: string | number | null | undefined): number | null {
  const raw = String(sceneNo ?? '').trim()
  if (!raw) return null
  if (/^\d+$/.test(raw)) return Number(raw)
  const m = raw.match(/(\d+)\s*$/)
  if (!m) return null
  return Number(m[1])
}

function normalizeScenePath(path: string): string {
  return String(path || '').trim().replace(/\\/g, '/')
}

function buildSceneVfsPath(scene: SceneItemDTO): string | null {
  const episodeNo = Number(scene.episode_no ?? 0)
  const sceneIndex = parseSceneIndex(scene.scene_no)
  if (!Number.isFinite(episodeNo) || episodeNo < 0) return null
  if (sceneIndex == null || sceneIndex <= 0 || sceneIndex > 999) return null
  const ep = String(Math.trunc(episodeNo)).padStart(2, '0')
  const sn = String(Math.trunc(sceneIndex)).padStart(3, '0')
  return `scenes/E${ep}-S${sn}.txt`
}

function findSceneByPathInCache(
  scenes: SceneItemDTO[],
  rawPath: string,
): SceneItemDTO | null {
  const path = normalizeScenePath(rawPath)
  if (!path) return null

  // 兼容旧路径：path 直接是 scene_id
  const byId = scenes.find((s) => String(s.id) === path)
  if (byId) return byId

  // 新路径：scenes/E03-S005.txt
  const m = path.match(VFS_SCENE_PATH_RE)
  if (!m) return null
  const ep = Number(m[1])
  const sc = Number(m[2])
  if (!Number.isFinite(ep) || !Number.isFinite(sc)) return null
  return (
    scenes.find(
      (s) =>
        Number(s.episode_no ?? 0) === ep &&
        Number(parseSceneIndex(s.scene_no) ?? -1) === sc,
    ) || null
  )
}

export function resolveScenePathAliases(workspaceId: string, rawPath: string): string[] {
  const normalizedPath = normalizeScenePath(rawPath)
  if (!normalizedPath) return []
  const aliases: string[] = [normalizedPath]
  const scenes = sceneCache.get(workspaceId)
  if (!Array.isArray(scenes) || scenes.length === 0) return aliases

  const scene = findSceneByPathInCache(scenes, normalizedPath)
  if (!scene) return aliases

  const sceneId = normalizeScenePath(String(scene.id))
  if (sceneId && !aliases.includes(sceneId)) {
    aliases.push(sceneId)
  }
  const vfsPath = buildSceneVfsPath(scene)
  if (vfsPath && !aliases.includes(vfsPath)) {
    aliases.push(vfsPath)
  }
  return aliases
}

async function ensureSceneCache(
  workspaceId: string,
  options?: AxiosRequestConfig,
  forceRefresh = false,
): Promise<SceneItemDTO[]> {
  let scenes = sceneCache.get(workspaceId)
  if (forceRefresh || !Array.isArray(scenes)) {
    await fetchWorkspaceFiles({ workspaceId }, options)
    scenes = sceneCache.get(workspaceId)
  }
  return Array.isArray(scenes) ? scenes : []
}

/**
 * 把 LLM 输出里的引用（"5-3"、"5-3 场"、"第 5 集第 3 场"）解析为 sceneId。
 *
 * 匹配策略（按强到弱）：
 *   1. scene_label 完全相等
 *   2. 形如 "{episode}-{scene_no}" → 找 episode_no=5 且 scene_no=3 的 scene
 *   3. 单纯 "{scene_no}" → 工作区只有一集时取该 scene_no
 *
 * 找不到返回 null（调用方决定 toast 还是静默）。
 *
 * 注意：前端 scene_no 在 DTO 里已经是数字字符串。
 */
/**
 * 按 scene_id（UUID）精确查找。
 *
 * 与 `findSceneByRef` 的区别：
 *   - findSceneByRef 接受 LLM 输出里的人类可读引用（"5-3" / "第5集第3场" / scene_label）；
 *   - findSceneById 接受报告里 evidence_refs[].scene_id（UUID），任务派发器走这条；
 *
 * 用 UUID 走 findSceneByRef 会误判"不存在"——它的匹配规则只认场号字符串。
 */
export function findSceneById(
  workspaceId: string,
  sceneId: string,
): SceneItemDTO | null {
  const scenes = sceneCache.get(workspaceId)
  if (!scenes || scenes.length === 0) return null
  const id = (sceneId || '').trim()
  if (!id) return null
  return scenes.find((s) => String(s.id) === id) || null
}

export function findSceneByRef(
  workspaceId: string,
  ref: string,
): SceneItemDTO | null {
  const scenes = sceneCache.get(workspaceId)
  if (!scenes || scenes.length === 0) return null
  const trimmed = ref.trim()
  if (!trimmed) return null

  const labelMatch = scenes.find(
    (s) => (s.scene_label || '').trim() === trimmed,
  )
  if (labelMatch) return labelMatch

  // {episode}-{scene_no}
  const m = trimmed.match(/^(\d+)\s*-\s*(\d+)$/)
  if (m) {
    const ep = Number(m[1])
    const sn = Number(m[2])
    const exact = scenes.find(
      (s) =>
        (s.episode_no ?? 0) === ep &&
        Number(parseSceneIndex(s.scene_no) ?? -1) === sn,
    )
    if (exact) return exact
  }

  // 仅 {scene_no}：单集时唯一匹配
  if (/^\d+$/.test(trimmed)) {
    const targetSceneNo = Number(trimmed)
    const onlyEp = new Set(scenes.map((s) => s.episode_no ?? 0))
    if (onlyEp.size === 1) {
      const hit = scenes.find(
        (s) => Number(parseSceneIndex(s.scene_no) ?? -1) === targetSceneNo,
      )
      if (hit) return hit
    }
  }

  return null
}

function scenesToFileTree(scenes: SceneItemDTO[]): DocStudioAPI.FileNode[] {
  if (!Array.isArray(scenes) || scenes.length === 0) return []

  // 按 episode_no 分组（null 归到 "__ep_0"）
  const groups = new Map<number, SceneItemDTO[]>()
  for (const s of scenes) {
    const ep = s.episode_no ?? 0
    if (!groups.has(ep)) groups.set(ep, [])
    groups.get(ep)!.push(s)
  }

  const epKeys = Array.from(groups.keys()).sort((a, b) => a - b)

  // 单集时摊平，不要无谓的目录层
  if (epKeys.length === 1) {
    return groups
      .get(epKeys[0])!
      .map<DocStudioAPI.FileNode>((s) => ({
        name: sceneFileName(s),
        path: s.id,
        type: 'file',
      }))
  }

  return epKeys.map<DocStudioAPI.FileNode>((ep) => ({
    name: ep === 0 ? '未知集' : `第 ${ep} 集`,
    path: `__ep_${ep}`,
    type: 'directory',
    children: groups
      .get(ep)!
      .map<DocStudioAPI.FileNode>((s) => ({
        name: sceneFileName(s),
        path: s.id,
        type: 'file',
      })),
  }))
}

function detailToWorkspace(d: ScriptDetailDTO): DocStudioAPI.WorkspaceDetail {
  return {
    workspaceId: d.id,
    name: d.title,
    mainFile: undefined, // 由 fetchWorkspaceFiles 时由前端选首个 scene 决定
    fileCount: d.total_scenes ?? 0,
    updatedAt: toUnixMs(d.updated_at),
    config: {
      title: d.title,
      source_format: d.source_format,
      status: d.status,
      total_episodes: d.total_episodes,
      total_scenes: d.total_scenes,
      total_chars: d.total_chars,
      failure_reason: d.failure_reason,
    },
  }
}

// ============================================================
// 工作区列表 / 详情 / 创建（能对齐到 ScriptLens 端点）
// ============================================================

export async function listWorkspaces(options?: AxiosRequestConfig) {
  const { data } = await request.get<ScriptListItemDTO[]>('', withScripts(options))
  if (!Array.isArray(data)) return []
  return data.map<DocStudioAPI.WorkspaceSummary>((item) => ({
    workspaceId: item.id,
    name: item.title,
    mainFile: undefined,
    fileCount: item.total_scenes ?? 0,
    updatedAt: toUnixMs(item.created_at),
  }))
}

export async function fetchWorkspace(
  params: { workspaceId: string },
  options?: AxiosRequestConfig,
) {
  const { data } = await request.get<ScriptDetailDTO>(
    `/${params.workspaceId}`,
    withScripts(options),
  )
  return detailToWorkspace(data)
}

/**
 * doc-studio 原意：先 createWorkspace 再 uploadFile；ScriptLens 是
 * `POST /upload` 一步上传文件。这里要求调用方在 `config.file` 里塞一个 File
 * 对象（doc-studio UI 上传流程之后会改造，这里只为不挡跑）。
 *
 * 没有 file 就 fail aloud。
 */
export async function createWorkspace(
  params: { name: string; workspaceId?: string; config?: Record<string, any> & { file?: File } },
  options?: AxiosRequestConfig,
) {
  const file = params.config?.file
  if (!file || !(file instanceof File)) {
    unsupported('createWorkspace 需要在 config.file 中提供文件；ScriptLens 不支持空工作区')
  }
  // 后端 /upload 只接收 multipart 'file' 字段，title 从文件名 stem 自动派生
  const fd = new FormData()
  fd.append('file', file)

  const { data } = await request.post<ScriptUploadResponseDTO>(
    '/upload',
    fd,
    withScripts({
      ...options,
      headers: {
        'Content-Type': 'multipart/form-data',
        ...(options?.headers ?? {}),
      },
    }),
  )

  return {
    workspaceId: data.id,
    name: data.title,
    mainFile: undefined,
    fileCount: 0,
    updatedAt: Date.now(),
    config: {
      title: data.title,
      source_format: data.source_format,
      status: data.status,
    },
  } as DocStudioAPI.WorkspaceDetail
}

export async function updateWorkspace(
  _params: {
    workspaceId: string
    name?: string
    config?: Record<string, any>
  },
  _options?: AxiosRequestConfig,
): Promise<DocStudioAPI.WorkspaceDetail> {
  return unsupported('updateWorkspace（剧本元数据修改未实现）')
}

export async function bindWorkspaceSession(
  params: { workspaceId: string; sessionId?: string | null },
  options?: AxiosRequestConfig,
): Promise<DocStudioAPI.WorkspaceDetail> {
  // ScriptLens chat 当前是无状态（history 由前端传），sessionId 无意义。
  // 直接返回当前 workspace 详情，让 doc-studio UI 的 session 绑定流程跑通。
  return fetchWorkspace({ workspaceId: params.workspaceId }, options)
}

export async function deleteWorkspace(
  params: { workspaceId: string },
  options?: AxiosRequestConfig,
): Promise<{
  deleted: boolean
  workspace_id: string
  storage_deleted: boolean
  deleted_counts: Record<string, number>
}> {
  const { data } = await request.delete<{
    deleted: boolean
    script_id: string
    title: string
    storage_deleted: boolean
    deleted_counts: Record<string, number>
  }>(`/${params.workspaceId}`, withScripts(options))
  sceneCache.delete(params.workspaceId)
  return {
    deleted: Boolean(data.deleted),
    workspace_id: data.script_id,
    storage_deleted: Boolean(data.storage_deleted),
    deleted_counts: data.deleted_counts || {},
  }
}

// ============================================================
// 文件树 / 文件内容（scenes 1:1 映射）
// ============================================================

export async function fetchWorkspaceFiles(
  params: { workspaceId: string },
  options?: AxiosRequestConfig,
) {
  const { data } = await request.get<ScenesResponseDTO>(
    `/${params.workspaceId}/scenes`,
    withScripts(options),
  )
  const scenes = Array.isArray(data?.scenes) ? data.scenes : []
  sceneCache.set(params.workspaceId, scenes)

  const files = scenesToFileTree(scenes)
  const mainFile = scenes.length > 0 ? scenes[0].id : undefined

  return {
    workspaceId: params.workspaceId,
    files,
    mainFile,
    config: {
      total_scenes: data?.total ?? scenes.length,
    },
  } as DocStudioAPI.WorkspaceFilesResponse
}

export async function fetchFileContent(
  params: { workspaceId: string; path: string; forceRefresh?: boolean },
  options?: AxiosRequestConfig,
) {
  // path 兼容两种形态：
  // 1) 旧：scene_id
  // 2) 新：scenes/E03-S005.txt（ScriptVFS 虚拟路径）
  const normalizedPath = normalizeScenePath(params.path)
  if (!normalizedPath) {
    throw new Error('场景路径不能为空')
  }
  const scenes = await ensureSceneCache(
    params.workspaceId,
    options,
    Boolean(params.forceRefresh),
  )
  const scene = findSceneByPathInCache(scenes, normalizedPath)
  if (!scene) {
    throw new Error(`场景不存在: path=${normalizedPath}`)
  }
  const result: DocStudioAPI.FileContentResponse = {
    path: normalizedPath,
    content: scene.text || '',
    encoding: 'utf-8',
  }
  return result
}

export async function updateFileContent(
  params: {
    workspaceId: string
    path: string
    content: string
    encoding?: string
  },
  options?: AxiosRequestConfig,
): Promise<DocStudioAPI.SaveFileResponse> {
  // path 兼容 scene_id / ScriptVFS path。最终都映射到 scene_id 写 DB。
  const { workspaceId, content } = params
  const normalizedPath = normalizeScenePath(params.path)
  if (!normalizedPath) {
    throw new Error('updateFileContent: path 不能为空')
  }

  let scenes = await ensureSceneCache(workspaceId, options)
  const scene = findSceneByPathInCache(scenes, normalizedPath)
  if (!scene) {
    throw new Error(`updateFileContent: 场景不存在 path=${normalizedPath}`)
  }
  const sceneId = String(scene.id)
  const url = `${SCRIPTS_BASE}/${encodeURIComponent(workspaceId)}/scenes/${encodeURIComponent(sceneId)}/content`
  await request<{ scene_id: string; char_count: number }>({
    ...(options || {}),
    url,
    method: 'put',
    data: { content },
  })

  // 同步刷新 sceneCache，避免下次 fetchFileContent 取到旧文本
  const refreshedScenes = sceneCache.get(workspaceId)
  if (Array.isArray(refreshedScenes)) {
    const idx = refreshedScenes.findIndex((s) => String(s.id) === sceneId)
    if (idx >= 0) {
      refreshedScenes[idx] = { ...refreshedScenes[idx], text: content }
    }
  }

  return {
    path: normalizedPath,
    size: content.length,
    modified_at: Math.floor(Date.now() / 1000),
    encoding: 'utf-8',
  }
}

export async function createFileOrDirectory(
  _params: {
    workspaceId: string
    path: string
    type: 'file' | 'directory'
    content?: string
  },
  _options?: AxiosRequestConfig,
): Promise<DocStudioAPI.FileCreateResponse> {
  return unsupported('createFileOrDirectory（剧本场景不可新增）')
}

export async function deleteFile(
  _params: { workspaceId: string; path: string },
  _options?: AxiosRequestConfig,
): Promise<{ deleted: boolean; path: string }> {
  return unsupported('deleteFile（剧本场景不可删除）')
}

export async function renameFileOrDirectory(
  _params: {
    workspaceId: string
    sourcePath: string
    targetPath: string
  },
  _options?: AxiosRequestConfig,
): Promise<{
  moved: boolean
  sourcePath: string
  targetPath: string
  type: 'file' | 'directory'
}> {
  return unsupported('renameFileOrDirectory（剧本场景不可重命名）')
}

export async function uploadFile(
  _params: {
    workspaceId: string
    directory?: string
    file: File
  },
  _options?: AxiosRequestConfig,
): Promise<DocStudioAPI.UploadResponse> {
  return unsupported('uploadFile（场景级上传未实现，请用 createWorkspace 整本上传）')
}

// ============================================================
// 消息 / debug（chat 状态由前端维护，这里返空避免 UI 挂掉）
// ============================================================

export async function listWorkspaceMessages(
  _params: {
    workspaceId: string
    sessionId: string
    page?: number
    pageSize?: number
  },
  _options?: AxiosRequestConfig,
) {
  return {
    total: 0,
    page: 1,
    pageSize: 200,
    items: [] as Array<{
      message_id: string
      session_id: string
      user_question: string
      model_answer: string
      create_time: string
      retrieval_content?: string
    }>,
  }
}

export async function getWorkspaceMessagesDebug(
  _params: {
    workspaceId: string
    sessionId: string
  },
  _options?: AxiosRequestConfig,
) {
  return {
    session_id: _params.sessionId,
    items: [] as Array<{
      message_id: string
      content_length: number
      newline_count: number
      double_newline_count: number
      triple_plus_newline_count: number
      raw_repr_sample: string
      raw_with_markers: string
    }>,
  }
}

// ============================================================
// 知识库（ScriptLens 不暴露多知识库选择，UI 上当作空列表）
// ============================================================

export async function listAgentKnowledgeBases(_options?: AxiosRequestConfig) {
  const empty: DocStudioAPI.KnowledgeBaseSummary[] = []
  return empty
}

// ============================================================
// Agent 异步运行（chat / events / cancel）
//
// ScriptLens 是 `POST /api/scripts/{id}/chat (body) → SSE` 一步直连。
// doc-studio UI 走两步：runAgentTaskAsync 拿 runId → EventSource(events_url)。
// 这里把两步桥接到一步：
//   1. runAgentTaskAsync 不真正发请求，只生成一个本地 runId 并把 chat
//      请求体存到 sseClient 的 pendingArgs map
//   2. UI 后续 new EventSource(getAgentAsyncEventsUrl(...)) 会被替换为
//      `openScriptLensAgentStream(runId)` —— 真正发起 fetch+ReadableStream
//      并按 EventSource 接口分发事件
//
// HitL（respondAgentRunInteraction）当前 ScriptLens 后端不发
// interaction_required 事件，UI 上的危险操作确认在短剧场景用不到。
// ============================================================

const activeStreams = new Map<string, ScriptLensAgentStream>()

function generateRunId(): string {
  // 浏览器原生 crypto.randomUUID 已普遍可用
  if (typeof crypto !== 'undefined' && typeof (crypto as any).randomUUID === 'function') {
    return (crypto as any).randomUUID()
  }
  return `run-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

/**
 * 把 doc-studio UI 调用 EventSource 的 URL 替换为 ScriptLens 流的句柄。
 *
 * 注意签名是 string —— 但 doc-studio UI 拿到 URL 后立即 `new EventSource(url)`，
 * 我们在 doc-studio/index.tsx 改造点上把 `new EventSource(url)` 替换成
 * `openScriptLensAgentStream(runId)`，所以这里返回的 URL 实际只用作占位
 * （包含 runId 让上层可以解析回去）。
 */
export function getAgentAsyncEventsUrl(_workspaceId: string, runId: string): string {
  return `scriptlens-stream:${runId}`
}

/**
 * doc-studio UI 改造点直接调用：返回一个 EventSource-shim。
 */
export function openAsyncEventStream(runId: string): ScriptLensAgentStream {
  const stream = openScriptLensAgentStream(runId)
  activeStreams.set(runId, stream)
  return stream
}

export async function runAgentTask(
  _params: {
    workspaceId: string
    userIntent: string
    context?: Record<string, any>
    options?: Record<string, any>
    collectTrainingData?: boolean
    knowledgeBaseId?: number
    knowledgeBaseName?: string
  },
  _options?: AxiosRequestConfig,
): Promise<DocStudioAPI.AgentResponse> {
  return unsupported('runAgentTask 同步接口（请用 runAgentTaskAsync + SSE）')
}

export async function runAgentTaskAsync(
  params: {
    workspaceId: string
    userIntent: string
    context?: Record<string, any>
    options?: Record<string, any>
    collectTrainingData?: boolean
    knowledgeBaseId?: number
    knowledgeBaseName?: string
  },
  _options?: AxiosRequestConfig,
): Promise<{ runId: string; status?: string }> {
  if (!params.workspaceId) {
    throw new Error('runAgentTaskAsync: workspaceId 为空')
  }
  if (!params.userIntent || !params.userIntent.trim()) {
    throw new Error('runAgentTaskAsync: userIntent 为空')
  }
  const runId = generateRunId()
  // role：从 context 读取，doc-studio UI 没传就用 general（PRD 默认）
  const role = String(params.context?.role || 'general')
  rememberChatArgs(runId, {
    scriptId: params.workspaceId,
    question: params.userIntent,
    history: [],
    role,
    context: params.context,
  })
  return { runId, status: 'queued' }
}

export async function fetchAgentRunStatus(
  _params: {
    workspaceId: string
    runId: string
  },
  _options?: AxiosRequestConfig,
): Promise<{
  run_id: string
  status: string
  result?: DocStudioAPI.AgentResponse
  error?: string
  updated_at?: number
}> {
  // ScriptLens 不持久化 run；UI 只在 finally 兜底拉一次状态，这里返回
  // succeeded 让 UI 退出 loading 即可。
  return {
    run_id: _params.runId,
    status: 'succeeded',
  }
}

export async function cancelAgentRun(
  params: {
    workspaceId: string
    runId: string
  },
  _options?: AxiosRequestConfig,
): Promise<{ runId: string; status: string }> {
  const stream = activeStreams.get(params.runId)
  if (stream) {
    stream.close()
    activeStreams.delete(params.runId)
  }
  return { runId: params.runId, status: 'cancelled' }
}

export async function respondAgentRunInteraction(
  _params: {
    workspaceId: string
    runId: string
    interactionId: string
    decision: string
    note?: string
  },
  _options?: AxiosRequestConfig,
): Promise<{ runId: string; status: string; accepted: boolean; decision: string }> {
  // ScriptLens 后端当前不发 interaction_required 事件（短剧场景没有危险操作
  // 确认链路）。UI 不会触发这个调用；万一触发就 fail aloud。
  return unsupported('respondAgentRunInteraction（ScriptLens 不支持 HitL 危险操作确认）')
}

export async function confirmAgentRunAction(
  params: {
    workspaceId: string
    runId: string
    confirmationId: string
    decision: 'approve' | 'reject'
    note?: string
  },
  _options?: AxiosRequestConfig,
) {
  return respondAgentRunInteraction({
    workspaceId: params.workspaceId,
    runId: params.runId,
    interactionId: params.confirmationId,
    decision: params.decision,
    note: params.note,
  })
}

// ============================================================
// Operations（M4 timeline）：改写历史 / 快照 / 回退
// ============================================================

export async function listOperations(
  params: { workspaceId: string },
  options?: AxiosRequestConfig,
): Promise<DocStudioAPI.OperationSummary[]> {
  const { data } = await request.get<{
    script_id: string
    items: DocStudioAPI.OperationSummary[]
  }>(`/${params.workspaceId}/operations`, withScripts(options))
  return Array.isArray(data?.items) ? data.items : []
}

export async function revertOperation(
  params: {
    workspaceId: string
    operationId: string
    files?: string[]
  },
  options?: AxiosRequestConfig,
): Promise<DocStudioAPI.RevertOperationResponse> {
  const encodedOperationId = encodeURIComponent(params.operationId)
  const payload = Array.isArray(params.files) && params.files.length > 0
    ? { files: params.files }
    : {}
  const { data } = await request.post<{
    operation_id: string
    reverted_files: string[]
    deleted_files: string[]
    skipped_files: string[]
  }>(
    `/${params.workspaceId}/operations/${encodedOperationId}/revert`,
    payload,
    withScripts(options),
  )
  // 回滚会直接改 DB（scenes.text），这里统一刷新 sceneCache，避免后续 fetchFileContent 拿到旧缓存。
  await fetchWorkspaceFiles({ workspaceId: params.workspaceId }, { ...options, loading: false, errorToast: false })
  return {
    operation_id: data.operation_id,
    reverted_files: data.reverted_files || [],
    deleted_files: data.deleted_files || [],
    skipped_files: data.skipped_files || [],
  }
}

export async function restoreCheckpoint(
  _params: { workspaceId: string; runId: string },
  _options?: AxiosRequestConfig,
): Promise<{ run_id: string; restored_files: string[]; skipped_files: string[] }> {
  return unsupported('restoreCheckpoint（短剧场景无 checkpoint 概念）')
}

export async function rewindConversation(
  _params: {
    workspaceId: string
    keepUserTurns?: number
    beforeMessageId?: string
  },
  _options?: AxiosRequestConfig,
): Promise<{
  session_id?: string
  total_turns?: number
  kept_turns?: number
  deleted_turns?: number
}> {
  return unsupported('rewindConversation（chat 当前无服务端 session）')
}

export async function fetchOperationSnapshotFile(
  params: {
    workspaceId: string
    operationId: string
    filePath: string
    version?: 'before' | 'after'
  },
  options?: AxiosRequestConfig,
): Promise<DocStudioAPI.FileContentResponse> {
  const encodedOperationId = encodeURIComponent(params.operationId)
  const version = params.version || 'before'
  const { data } = await request.get<DocStudioAPI.FileContentResponse>(
    `/${params.workspaceId}/operations/${encodedOperationId}/snapshot`,
    withScripts({
      ...(options ?? {}),
      params: {
        file_path: params.filePath,
        version,
        ...(options?.params ?? {}),
      },
    }),
  )
  return data
}

// ============================================================
// 编译 / PDF / 下载（短剧业务无此概念）
// ============================================================

export async function compileWorkspace(
  _params: {
    workspaceId: string
    mainFile?: string
    compiler?: string
  },
  _options?: AxiosRequestConfig,
): Promise<DocStudioAPI.CompileResult> {
  return unsupported('compileWorkspace（短剧无编译概念，可改为触发 reanalyze）')
}

export async function fetchCompileStatus(
  _params: { workspaceId: string },
  _options?: AxiosRequestConfig,
): Promise<DocStudioAPI.CompileStatus> {
  // 返回空闲态让 UI 不显示"编译中"
  return { status: 'idle' }
}

export function buildDownloadUrl(_workspaceId: string, _filePath: string): string {
  unsupported('buildDownloadUrl（场级下载未实现）')
}

export function buildPdfUrl(_workspaceId: string, _pdfPath?: string): string {
  unsupported('buildPdfUrl（短剧无 PDF 编译产物）')
}

export async function downloadPdf(
  _params: { workspaceId: string; pdfPath?: string },
  _options?: AxiosRequestConfig,
): Promise<Blob> {
  return unsupported('downloadPdf（短剧无 PDF 编译产物）')
}

export async function downloadFile(
  _params: { workspaceId: string; filePath: string },
  _options?: AxiosRequestConfig,
): Promise<Blob> {
  return unsupported('downloadFile（场级下载未实现）')
}

// ============================================================
// Report（5 维评分报告：fetch / reanalyze）—— ScriptLens 独有
// ============================================================

export async function fetchScriptReport(
  scriptId: string,
  options?: AxiosRequestConfig,
): Promise<ReportFetchResult> {
  const { data } = await request.get<ReportFetchResult>(
    `/${scriptId}/report`,
    withScripts(options),
  )
  return data
}

export async function reanalyzeScript(
  scriptId: string,
  options?: AxiosRequestConfig,
): Promise<{ script_id: string; status: string }> {
  const { data } = await request.post<{ script_id: string; status: string }>(
    `/${scriptId}/reanalyze`,
    {},
    withScripts(options),
  )
  return data
}

// ============================================================
// Report 进度（与 backend service.script_progress_tracker 对齐）
// ============================================================

export type ReportStageState = 'pending' | 'running' | 'done' | 'failed'

export interface ReportStageDTO {
  id: string
  label: string
  description: string
  state: ReportStageState
  detail?: string | null
  started_at?: number | null
  completed_at?: number | null
}

export interface ReportProgressSnapshotDTO {
  script_id: string
  started_at: number
  updated_at: number
  final: boolean
  error?: string | null
  current_index: number
  stages: ReportStageDTO[]
}

export interface ReportProgressResponseDTO {
  script_id: string
  snapshot: ReportProgressSnapshotDTO | null
}

export async function fetchScriptReportProgress(
  scriptId: string,
  options?: AxiosRequestConfig,
): Promise<ReportProgressResponseDTO> {
  const { data } = await request.get<ReportProgressResponseDTO>(
    `/${scriptId}/progress`,
    withScripts({
      // 进度查询是高频轮询，4xx/5xx 全部走 UI 自己的 fallback，不要弹全局 toast
      errorToast: false,
      ...(options ?? {}),
    }),
  )
  return data
}

// ============================================================
// View：返回报告全字段（含派生 rewrite_seeds / task_status）
// 视角切换由前端「行动」segment 派生 Persona Action Card（详见 docs/09-action-lens.md）
// ============================================================

/**
 * 改写候选种子（任务派发器入口）。
 *
 * 报告**只产候选定位 + 触发**，不预生成 rewritten_excerpt。详见 docs/03-system-mental-model.md §6。
 * 真正的 original/rewritten/diff/rationale 由用户在 chat 触发 propose_rewrite_tool 实时生产。
 */
export interface RewriteSeedDTO {
  dimension: DimensionKey
  scene_id: string
  scene_label?: string | null
  issue: string
  evidence_ref_id: string
}

/**
 * 单个 (scene_id, dimension) 上的改写任务状态（从 script_operations 派生）。
 *
 * 前端按 task_status[`${scene_id}:${dimension}`] lookup，渲染卡片右上角状态徽章。
 * 状态映射详见 docs/03-system-mental-model.md §8。
 */
export interface RewriteTaskStatusDTO {
  attempts: number
  last_op_id: string | null
  last_status: 'proposed' | 'accepted' | 'rejected' | null
  last_at: string | null
}

export interface ScriptViewResponseDTO {
  script_id: string
  decision: DecisionCardDTO
  overall_score: number | null
  summary: string
  scorecard: ScorecardItemDTO[]
  /** 合规审核独立字段（透传自 ReportPayload.compliance） */
  compliance?: ComplianceDTO | null
  must_read_scene_ids: string[]
  risk_flags: RiskFlagDTO[]
  evidence_refs: EvidenceRefDTO[]
  /** 主要看点 / 钩子 / 反转 / 爽点列表（透传自 ReportPayload.highlights） */
  highlights?: HighlightDTO[]
  /** v3 速览：logline + recommendation + 优劣点 */
  coverage_card?: CoverageCardDTO | null
  /** v3 故事：三幕骨架 + 关键节拍 */
  beat_sheet?: BeatSheetDTO | null
  /** v3 人物：角色节点 + 关系边 */
  character_graph?: CharacterGraphDTO | null
  /** v3 故事：每集事件密度 + 情感弧 */
  pacing_curve?: PacingCurvePointDTO[]
  /** v3 评估：5 维评分 + 风险 + 改写候选 */
  evaluation?: EvaluationPayloadDTO | null
  /** 派生：「最值得改的 N 场」候选（详见 docs/03-system-mental-model.md §6） */
  rewrite_seeds: RewriteSeedDTO[]
  /** 派生：每个 (scene, dim) 上的改写任务状态，key=`${scene_id}:${dimension}` */
  task_status: Record<string, RewriteTaskStatusDTO>
}

export async function fetchScriptView(
  scriptId: string,
  options?: AxiosRequestConfig,
): Promise<ScriptViewResponseDTO> {
  const { data } = await request.get<ScriptViewResponseDTO>(
    `/${scriptId}/view`,
    withScripts(options),
  )
  return data
}

export async function fetchScriptDetail(
  scriptId: string,
  options?: AxiosRequestConfig,
): Promise<ScriptDetailDTO> {
  const { data } = await request.get<ScriptDetailDTO>(
    `/${scriptId}`,
    withScripts(options),
  )
  return data
}

// ============================================================
// Rewrite（场景改写：同步接口，返回 original / rewritten / diff）
// ============================================================

// 改写聚焦维度（阅文五力，docs/08 §3）；合规违规不通过 LLM 改写，不在此枚举
export type RewriteDimension =
  | 'story'
  | 'character'
  | 'concept'
  | 'emotion'
  | 'pacing'

export interface RewriteRequestPayload {
  scene_id: string
  target_dimension: RewriteDimension
  issue: string
}

export interface RewriteResponseDTO {
  script_id: string
  scene_id: string
  target_dimension: RewriteDimension
  issue: string
  original_text: string
  rewritten_text: string
  rationale: string
  diff: string
}

export async function rewriteScript(
  scriptId: string,
  payload: RewriteRequestPayload,
  options?: AxiosRequestConfig,
): Promise<RewriteResponseDTO> {
  const { data } = await request.post<RewriteResponseDTO>(
    `/${scriptId}/rewrite`,
    payload,
    withScripts(options),
  )
  return data
}

// ============================================================
// F2/F3：导出完整剧本（已应用所有 script_operations 的最新版本）
// ============================================================

export type ScriptExportFormat = 'docx' | 'pdf' | 'txt'

/**
 * 触发后端导出 → 浏览器下载文件。
 * 后端 GET /api/scripts/{id}/export?format=docx|pdf|txt 返回二进制流；
 * 前端用 axios responseType=blob 接收，再用临时 <a> 触发下载。
 */
export async function exportFullScript(
  scriptId: string,
  format: ScriptExportFormat,
): Promise<void> {
  const response = await request.get<Blob>(
    `/${scriptId}/export`,
    withScripts({
      params: { format },
      responseType: 'blob',
    }),
  )
  const blob = response.data as unknown as Blob
  const cd = (response.headers as Record<string, string>)?.['content-disposition'] || ''
  // Content-Disposition: attachment; filename="xxx.docx"
  const match = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i)
  const filename = match ? decodeURIComponent(match[1]) : `script.${format}`
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

// ============================================================
// Feedback（PRD §10 P3 skill 机制）—— ScriptLens-native schema
// ============================================================

export type ScriptFeedbackScope = 'general' | 'dimension' | 'rewrite' | 'scene'

export interface ScriptFeedbackPayload {
  scope: ScriptFeedbackScope
  scope_ref?: string | null
  message: string
}

export interface ScriptFeedbackItem {
  id: string
  scope: ScriptFeedbackScope
  scope_ref?: string | null
  message: string
  created_at: string
}

export async function submitScriptFeedback(
  scriptId: string,
  payload: ScriptFeedbackPayload,
  options?: AxiosRequestConfig,
): Promise<ScriptFeedbackItem> {
  const { data } = await request.post<ScriptFeedbackItem>(
    `/${scriptId}/feedback`,
    payload,
    withScripts(options),
  )
  return data
}

export async function fetchScriptFeedback(
  scriptId: string,
  limit = 50,
  options?: AxiosRequestConfig,
): Promise<{ script_id: string; items: ScriptFeedbackItem[] }> {
  const { data } = await request.get<{ script_id: string; items: ScriptFeedbackItem[] }>(
    `/${scriptId}/feedback`,
    withScripts({
      ...(options ?? {}),
      params: { limit, ...(options?.params ?? {}) },
    }),
  )
  return data
}

// ============================================================
// 反馈（部分对齐：traceId 当作 scope_ref，scope=general）
// ============================================================

export async function sendAgentFeedback(
  params: {
    traceId: string
    rating: DocStudioAPI.AgentFeedbackRating
    comment?: string
    scriptId?: string
  },
  _options?: AxiosRequestConfig,
) {
  if (!params.scriptId) {
    // doc-studio 原协议不带 scriptId，必须由调用方补；不补就 fail aloud
    unsupported('sendAgentFeedback 需要 scriptId（请在调用处传入当前 workspaceId）')
  }
  return request.post(
    `/${params.scriptId}/feedback`,
    {
      scope: 'general',
      scope_ref: params.traceId,
      message: `[${params.rating}]${params.comment ? ' ' + params.comment : ''}`,
    },
    withScripts(),
  )
}

// ============================================================
// 监控（UI 上的 metrics / llm health 面板，业务无关，返空）
// ============================================================

export async function fetchMetricsSummary(
  _options?: AxiosRequestConfig,
): Promise<DocStudioAPI.MetricsSummary> {
  return {
    tools: {},
    intents: {},
    plans: {},
    workspace_scans: { count: 0, total_duration_seconds: 0 },
    workspace_cache_events: {},
    feedback: {},
  }
}

export async function fetchLlmHealth(
  _options?: AxiosRequestConfig,
): Promise<DocStudioAPI.LlmHealthSummary> {
  return {
    providers: [],
    fallback_enabled: false,
    fallback_allow_explicit_provider: false,
    failure_threshold: 0,
    cooldown_seconds: 0,
    request_timeout: 0,
  }
}
