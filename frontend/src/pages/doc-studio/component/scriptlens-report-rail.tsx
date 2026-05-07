/**
 * ScriptLens · doc-studio 右栏「分析报告」面板。
 *
 * 心智模型详见 docs/03-system-mental-model.md：
 *   - 报告 = 解决「不能只看摘要、又没时间读全文」的核心阅读层（task.md §三）
 *   - 信息分层：顶部 30 秒判断 → 中段 5 分钟核心理解 → 底部深度评分 / 改写候选
 *   - 报告里所有可点元素分两类：
 *       a) 溯源（evidence chip / 关键场景 / 看点条）→ 跳原文 + 双向持久高亮，**不派 Agent**
 *       b) 改写 / 追问（"让 Agent 改写" / 维度 search 按钮）→ 切 chat tab 派 AgentTask
 *
 * 数据源：fetchScriptView(role='selection')，view 包含派生的 rewrite_seeds + task_status。
 */

import {
  EditOutlined,
  ExpandOutlined,
  ReloadOutlined,
  SearchOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Modal,
  Progress,
  Segmented,
  Skeleton,
  Space,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import axios from 'axios'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import { forceCollide as forceCollide2D } from 'd3-force'
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import {
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  ToolboxComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import ReactECharts from 'echarts-for-react/lib/core'

echarts.use([GraphChart, TooltipComponent, LegendComponent, TitleComponent, ToolboxComponent, CanvasRenderer])
import {
  fetchScriptDetail,
  fetchScriptView,
  reanalyzeScript,
  type BeatActDTO,
  type CharacterGraphEdgeDTO,
  type CharacterGraphNodeDTO,
  type ComplianceDTO,
  type EvidenceRefDTO,
  type HighlightDTO,
  type HighlightType,
  type RewriteSeedDTO,
  type RewriteTaskStatusDTO,
  type ScorecardItemDTO,
  type ScriptViewRole,
  type ScriptViewResponseDTO,
} from '@/api/docStudio'
import { formatSceneLocator, type AgentTask } from '../agentTask'
import ScriptlensReportProgress, {
  ProgressFallbackPanel,
} from './scriptlens-report-progress'
import styles from './scriptlens-report-rail.module.scss'

const { Text, Paragraph } = Typography

// ============================================================
// 元数据
// ============================================================

// 阅文五力（docs/08-evaluation-framework.md §3）；合规独立用 COMPLIANCE_LABEL
const DIMENSION_LABELS: Record<string, string> = {
  story: '故事力',
  character: '人物力',
  concept: '题材力',
  emotion: '情感力',
  pacing: '叙事力',
  compliance: '合规审核',
}

const DIMENSION_HINTS: Record<string, string> = {
  story: '主线清晰度 + 反转密度',
  character: '主角动机弧光 + 关键关系冲突',
  concept: '赛道辨识 + 卖点钩子',
  emotion: '情感钩子 + 爽点密度',
  pacing: '开场速度 + 节奏方差',
  compliance: '广电八关 + 6 类红线',
}

const LEVEL_COLOR: Record<string, string> = {
  high: 'green',
  medium: 'orange',
  low: 'red',
  clean: 'cyan',
  high_risk: 'red',
  medium_risk: 'orange',
  low_risk: 'gold',
}

const LEVEL_LABEL: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
  clean: '安全',
  high_risk: '高风险',
  medium_risk: '中风险',
  low_risk: '低风险',
}

const DECISION_LABEL: Record<string, { text: string; color: string }> = {
  recommend: { text: '推荐立项', color: 'green' },
  recommend_continue: { text: '推荐立项', color: 'green' },
  cautious_continue: { text: '审慎推进', color: 'orange' },
  refer_for_rewrite: { text: '建议改写', color: 'gold' },
  not_recommend: { text: '不建议立项', color: 'red' },
  not_recommended: { text: '不建议立项', color: 'red' },
}

// 看点 / 钩子 / 反转 / 爽点：UI 标签 + 颜色（低饱和莫兰迪暖色系）
const HIGHLIGHT_LABEL: Record<HighlightType, string> = {
  hook: '钩子',
  face_slap: '打脸',
  reversal: '反转',
  revenge: '复仇',
  cp_progress: 'CP 进展',
  identity_reveal: '身份揭露',
  villain_fall: '反派败落',
  underdog_rise: '逆袭',
  scheme_exposed: '阴谋败露',
  risk: '风险点',
}

const HIGHLIGHT_COLOR: Record<HighlightType, string> = {
  hook: 'magenta',
  face_slap: 'volcano',
  reversal: 'gold',
  revenge: 'red',
  cp_progress: 'pink',
  identity_reveal: 'purple',
  villain_fall: 'orange',
  underdog_rise: 'green',
  scheme_exposed: 'geekblue',
  risk: 'red',
}

// ============================================================
// 类型 / Props
// ============================================================

type LoadState =
  | { phase: 'loading' }
  | { phase: 'not_ready'; status: string; failureReason?: string | null }
  | { phase: 'no_report'; alreadyTriggered?: boolean }
  | { phase: 'ready'; view: ScriptViewResponseDTO; scriptTitle: string }
  | { phase: 'error'; error: string }

type ScriptDetailStatus = {
  id: string
  title: string
  source_format?: string | null
  status: string
  total_episodes?: number | null
  total_scenes?: number | null
  total_chars?: number | null
  failure_reason?: string | null
}

interface Props {
  scriptId: string
  /** 当前激活的 evidence id（doc-studio 维护，rail 用来同色高亮对齐） */
  activeEvidenceId: string | null
  /** 溯源动作：跳原文 + 持久高亮，**不派 Agent** */
  onTraceEvidence: (params: {
    evidenceRefId: string
    sceneId: string
    startLine?: number | null
    endLine?: number | null
  }) => void | Promise<void>
  /** 显式取消溯源（清掉编辑器持久高亮） */
  onClearTrace: () => void
  /** 派 Agent：仅 rewrite_seed / dim_inquiry 走这条 */
  onDispatchTask: (task: AgentTask) => void
  /** 把右栏拉到的新鲜 status 同步回 doc-studio 主区，避免两边状态显示错位。 */
  onScriptDetailLoaded?: (detail: ScriptDetailStatus) => void
}

/**
 * 模块级 view cache（按 scriptId 隔离）。
 *
 * 解决「切右栏 tab 时 rail 被 unmount → 重 mount 触发 loadOnce → 闪 loading skeleton」的问题：
 *   - 已经成功拉过一次 view 的剧本，再次 mount 时直接复用缓存 → phase=ready 立刻显示
 *   - 后台仍然 loadOnce 一次刷新数据，但不再回到 loading 态，UI 无闪烁
 */
const VIEW_CACHE_TTL_MS = 60_000
const viewCache = new Map<string, { view: ScriptViewResponseDTO; scriptTitle: string; cachedAt: number }>()

const ROLE_OPTIONS: Array<{ label: string; value: ScriptViewRole; hint: string }> = [
  { label: '选品视角', value: 'selection', hint: '先看能不能抓人、值不值得继续跟' },
  { label: '编剧视角', value: 'writer', hint: '先看人物动机、结构节奏和可改写点' },
  { label: '审核视角', value: 'review', hint: '先看风险、价值观红线和可优化表达' },
]

type ReportMode = 'overview' | 'story' | 'characters' | 'evaluation'

const REPORT_MODE_OPTIONS: Array<{ label: string; value: ReportMode }> = [
  { label: '速览', value: 'overview' },
  { label: '故事', value: 'story' },
  { label: '人物', value: 'characters' },
  { label: '评估', value: 'evaluation' },
]

const SCRIPT_STATUS_LABELS: Record<string, string> = {
  pending: '排队中',
  parsing: '文本解析与集场切分',
  indexing: '索引入库',
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

function cacheKey(scriptId: string, role: ScriptViewRole): string {
  return `${scriptId}:${role}`
}

// ============================================================
// 组件主体
// ============================================================

export default function ScriptlensReportRail({
  scriptId,
  activeEvidenceId,
  onTraceEvidence,
  onClearTrace,
  onDispatchTask,
  onScriptDetailLoaded,
}: Props) {
  const [role, setRole] = useState<ScriptViewRole>('selection')
  const [state, setState] = useState<LoadState>(() => {
    const cached = viewCache.get(cacheKey(scriptId, 'selection'))
    if (cached && Date.now() - cached.cachedAt < VIEW_CACHE_TTL_MS) {
      return { phase: 'ready', view: cached.view, scriptTitle: cached.scriptTitle }
    }
    return { phase: 'loading' }
  })
  const [reanalyzing, setReanalyzing] = useState(false)
  const pollTimerRef = useRef<number | null>(null)

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }, [])

  const loadOnce = useCallback(async (): Promise<boolean> => {
    if (!scriptId) return true
    try {
      const detail = await fetchScriptDetail(scriptId)
      onScriptDetailLoaded?.(detail)
      if (detail.status !== 'ready') {
        setState({ phase: 'not_ready', status: detail.status, failureReason: detail.failure_reason })
        return false
      }
      try {
        const view = await fetchScriptView(scriptId, role, { errorToast: false })
        setState({ phase: 'ready', view, scriptTitle: detail.title })
        viewCache.set(cacheKey(scriptId, role), { view, scriptTitle: detail.title, cachedAt: Date.now() })
        return true
      } catch (err: unknown) {
        const status = (err as { response?: { status?: number } })?.response?.status
        if (status === 409) {
          setState({ phase: 'no_report' })
          return false
        }
        throw err
      }
    } catch (err: unknown) {
      if (axios.isCancel(err)) return false
      const e = err as { response?: { data?: { detail?: string } }; message?: string }
      setState((prev) => {
        if (prev.phase === 'ready') return prev
        return { phase: 'error', error: e?.response?.data?.detail || e?.message || '加载失败' }
      })
      return true
    }
  }, [scriptId, role, onScriptDetailLoaded])

  useEffect(() => {
    let cancelled = false
    const cached = viewCache.get(cacheKey(scriptId, role))
    if (cached && Date.now() - cached.cachedAt < VIEW_CACHE_TTL_MS) {
      setState({ phase: 'ready', view: cached.view, scriptTitle: cached.scriptTitle })
    } else {
      setState({ phase: 'loading' })
    }
    const tick = async () => {
      if (cancelled) return
      const done = await loadOnce()
      if (done || cancelled) return
      pollTimerRef.current = window.setTimeout(tick, 3000)
    }
    void tick()
    return () => {
      cancelled = true
      stopPolling()
    }
  }, [scriptId, role, loadOnce, stopPolling])

  // 重新诊断 == 上传后那条全链路重新跑一遍：清缓存 + 切到 no_report 阶段，
  // 让 ScriptlensReportProgress 接管，把 6 阶段实时进度面板替换掉旧报告。
  const handleReanalyze = useCallback(async () => {
    if (!scriptId || reanalyzing) return
    stopPolling()
    setReanalyzing(true)
    try {
      await reanalyzeScript(scriptId)
      // 清掉本剧本所有视角的缓存，避免轮询时 cache hit 又把旧报告闪回来
      for (const r of ['selection', 'writer', 'review'] as ScriptViewRole[]) {
        viewCache.delete(cacheKey(scriptId, r))
      }
      // 关键：切到 no_report 阶段触发 <ScriptlensReportProgress>，与首次上传完全一致；
      // alreadyTriggered=true 让 progress 组件不要再 auto-trigger 一次造成竞态
      setState({ phase: 'no_report', alreadyTriggered: true })
    } catch (err: unknown) {
      const e = err as { response?: { data?: { detail?: string } }; message?: string }
      message.error(`触发重新诊断失败：${e?.response?.data?.detail || e?.message || '未知错误'}`)
    } finally {
      setReanalyzing(false)
    }
  }, [scriptId, reanalyzing, stopPolling])

  // ====================== 渲染分支 ======================

  if (state.phase === 'loading') {
    return (
      <div className={styles.rail}>
        <Skeleton active paragraph={{ rows: 6 }} />
      </div>
    )
  }

  if (state.phase === 'error') {
    return (
      <div className={styles.rail}>
        <Alert type="error" showIcon message="加载报告失败" description={state.error} />
        <Button onClick={() => loadOnce()} icon={<ReloadOutlined />} size="small" style={{ marginTop: 12 }}>
          重试
        </Button>
      </div>
    )
  }

  if (state.phase === 'not_ready') {
    const isFailed = state.status === 'failed'
    const label = SCRIPT_STATUS_LABELS[state.status] || '准备中'
    const percent = SCRIPT_STATUS_PROGRESS[state.status] ?? 28
    return (
      <div className={styles.rail}>
        {isFailed ? (
          <Alert
            type="error"
            showIcon
            message="剧本解析失败"
            description={state.failureReason || '后台未给出失败原因'}
          />
        ) : (
          <div className={styles.parseProgressCard}>
            <Tag color="pink">剧本解析中</Tag>
            <h3>正在准备整剧分析</h3>
            <Text type="secondary" className={styles.parseProgressDesc}>
              系统正在读取全文、切分集场并写入检索索引。完成后会自动进入整剧分析流程。
            </Text>
            <Progress
              percent={percent}
              status="active"
              strokeColor="#E07A8C"
              trailColor="#F7E9E5"
            />
            <div className={styles.parseProgressMeta}>
              <span>当前阶段：{label}</span>
              <span>每 3 秒自动刷新</span>
            </div>
          </div>
        )}
      </div>
    )
  }

  if (state.phase === 'no_report') {
    const alreadyTriggered = state.alreadyTriggered === true
    return (
      <div className={styles.rail} style={{ padding: 0 }}>
        <ScriptlensReportProgress
          scriptId={scriptId}
          compact
          onAutoTrigger={
            alreadyTriggered
              ? undefined
              : async () => {
                  try {
                    await reanalyzeScript(scriptId)
                  } catch {
                    // 触发失败时让 fallback 兜底
                  }
                }
          }
          onFinalized={(snap) => {
            if (snap.error) {
              message.warning(`整剧分析异常：${snap.error}`)
              return
            }
            void loadOnce()
          }}
          fallback={<ProgressFallbackPanel reanalyzing={reanalyzing} onReanalyze={handleReanalyze} />}
        />
      </div>
    )
  }

  return (
    <ReadyRail
      view={state.view}
      scriptTitle={state.scriptTitle}
      reanalyzing={reanalyzing}
      onReanalyze={handleReanalyze}
      activeEvidenceId={activeEvidenceId}
      role={role}
      onRoleChange={setRole}
      onTraceEvidence={onTraceEvidence}
      onClearTrace={onClearTrace}
      onDispatchTask={onDispatchTask}
    />
  )
}

// ============================================================
// 主体（已就绪态）
// ============================================================

interface ReadyRailProps {
  view: ScriptViewResponseDTO
  scriptTitle: string
  reanalyzing: boolean
  onReanalyze: () => void
  activeEvidenceId: string | null
  role: ScriptViewRole
  onRoleChange: (role: ScriptViewRole) => void
  onTraceEvidence: Props['onTraceEvidence']
  onClearTrace: () => void
  onDispatchTask: (task: AgentTask) => void
}

function ReadyRail({
  view,
  scriptTitle,
  reanalyzing,
  onReanalyze,
  activeEvidenceId,
  role,
  onRoleChange,
  onTraceEvidence,
  onClearTrace,
  onDispatchTask,
}: ReadyRailProps) {
  const [reportMode, setReportMode] = useState<ReportMode>('overview')
  const decisionInfo = DECISION_LABEL[view.decision.label] || {
    text: view.decision.label,
    color: 'default',
  }
  const overall = view.overall_score
  const overallColor =
    overall == null ? '#bfbfbf' : overall >= 7 ? '#52c41a' : overall >= 5 ? '#faad14' : '#ff4d4f'

  const evidenceMap = useMemo(() => {
    const m = new Map<string, EvidenceRefDTO>()
    for (const r of view.evidence_refs || []) m.set(r.id, r)
    return m
  }, [view.evidence_refs])

  const evidenceBySceneId = useMemo(() => {
    const m = new Map<string, EvidenceRefDTO>()
    for (const r of view.evidence_refs || []) {
      if (!m.has(r.scene_id)) m.set(r.scene_id, r)
    }
    return m
  }, [view.evidence_refs])

  // 描述卡：3-5 句剧本概览（来自 LLM 决策聚合产出的 summary）
  const summaryText = (view.decision.summary || view.summary || '').trim()

  // 主要看点（按 type 分组渲染）
  const highlights = (view.highlights || []) as HighlightDTO[]
  const highlightsGrouped = useMemo(() => groupHighlights(highlights), [highlights])
  const roleMeta = ROLE_OPTIONS.find((x) => x.value === role)
  const keySceneRefs = view.must_read_scene_ids
    .map((rid) => evidenceMap.get(rid))
    .filter((ref): ref is EvidenceRefDTO => Boolean(ref))

  const overviewHighlights = highlights.slice(0, 5)
  const riskCount = view.risk_flags?.length ?? 0
  const beatCount = view.beat_sheet?.acts?.reduce((sum, act) => sum + (act.beats?.length || 0), 0) ?? 0

  return (
    <div className={styles.rail}>
      {/* === 顶部 30 秒判断层：标题 + 决策 + 一句话理由 + 重跑按钮 === */}
      <div className={styles.titleRow}>
        <div className={styles.titleText} title={scriptTitle}>
          《{scriptTitle}》
        </div>
        <Tooltip title="重新诊断整剧：重跑速览 / 故事 / 人物 / 评估 全部分析链，覆盖旧报告">
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={reanalyzing}
            onClick={onReanalyze}
            className={styles.reanalyzeBtn}
          >
            重新诊断
          </Button>
        </Tooltip>
      </div>

      <div className={styles.roleSwitch}>
        <Segmented
          size="small"
          value={role}
          options={ROLE_OPTIONS.map((item) => ({ label: item.label, value: item.value }))}
          onChange={(next) => onRoleChange(next as ScriptViewRole)}
          block
        />
        {roleMeta ? <div className={styles.roleHint}>{roleMeta.hint}</div> : null}
      </div>

      <div className={styles.headlineCard}>
        <div className={styles.headlineRow}>
          <Tag color={decisionInfo.color} className={styles.decisionTag}>
            {decisionInfo.text}
          </Tag>
          {overall == null ? (
            <Text className={styles.overallNull}>综合评分 · 证据不足</Text>
          ) : (
            <Progress
              type="circle"
              percent={Math.round((overall / 10) * 100)}
              size={56}
              strokeColor={overallColor}
              format={() => (
                <span style={{ color: overallColor, fontSize: 16, fontWeight: 600 }}>
                  {overall.toFixed(1)}
                </span>
              )}
            />
          )}
        </div>
        <Paragraph className={styles.oneLineReason}>
          {humanizeReportText(view.decision.one_sentence_reason)}
        </Paragraph>
      </div>

      <div className={styles.reportModeSwitch}>
        <Segmented
          size="small"
          value={reportMode}
          options={REPORT_MODE_OPTIONS}
          onChange={(next) => setReportMode(next as ReportMode)}
          block
        />
      </div>

      {reportMode === 'overview' ? (
        <>
          <div className={styles.quickStats}>
            <span>关键人物 {view.character_graph?.nodes?.length ?? 0}</span>
            <span>关键场景 {keySceneRefs.length}</span>
            <span>故事节拍 {beatCount}</span>
            <span>风险标签 {riskCount}</span>
          </div>

          {view.coverage_card ? (
            <CoverageCardSection coverage={view.coverage_card} evidenceBySceneId={evidenceBySceneId} activeEvidenceId={activeEvidenceId} onTraceEvidence={onTraceEvidence} />
          ) : null}

          {summaryText ? (
            <section className={styles.summarySection}>
              <SectionHeader title="剧本概览" hint="先读这一段，判断是否值得继续看" />
              <Paragraph className={styles.summaryText}>{humanizeReportText(summaryText)}</Paragraph>
            </section>
          ) : null}

          {keySceneRefs.length > 0 ? (
            <KeyScenesSection
              evidences={keySceneRefs}
              activeEvidenceId={activeEvidenceId}
              onTraceEvidence={onTraceEvidence}
            />
          ) : null}

          {overviewHighlights.length > 0 ? (
            <HighlightsSection
              highlights={overviewHighlights}
              activeEvidenceId={activeEvidenceId}
              onTraceEvidence={onTraceEvidence}
              grouped={false}
              hint="前 5 个剧情抓手"
            />
          ) : null}
        </>
      ) : null}

      {reportMode === 'story' ? (
        <>
          {view.beat_sheet?.acts?.length ? (
            <BeatTimelineSection
              acts={view.beat_sheet.acts}
              evidenceBySceneId={evidenceBySceneId}
              activeEvidenceId={activeEvidenceId}
              onTraceEvidence={onTraceEvidence}
            />
          ) : null}

          {view.pacing_curve?.length ? (
            <PacingCurveSection points={view.pacing_curve} />
          ) : null}

          {highlights.length > 0 ? (
            <HighlightsSection
              highlights={highlights}
              activeEvidenceId={activeEvidenceId}
              onTraceEvidence={onTraceEvidence}
              grouped
              groups={highlightsGrouped}
              hint={`${highlights.length} 个节点 · 点击跳原文`}
            />
          ) : null}
        </>
      ) : null}

      {reportMode === 'characters' ? (
        <>
          {view.character_graph?.nodes?.length ? (
            <CharacterGraphSection
              nodes={view.character_graph.nodes}
              edges={view.character_graph.edges || []}
              evidenceBySceneId={evidenceBySceneId}
              activeEvidenceId={activeEvidenceId}
              onTraceEvidence={onTraceEvidence}
            />
          ) : null}
        </>
      ) : null}

      {reportMode === 'evaluation' ? (
        <>
          <section className={styles.scorecardSection}>
            <SectionHeader
              title="数据评估 · 阅文五力"
              hint="故事/人物/题材/情感/叙事 五维评分（合规独立见下方），点证据→跳原文，点 🔍→让 Agent 解释"
            />
            <Space direction="vertical" size={8} style={{ width: '100%', marginTop: 8 }}>
              {view.scorecard.map((item) => (
                <DimCard
                  key={item.dimension}
                  item={item}
                  evidenceMap={evidenceMap}
                  activeEvidenceId={activeEvidenceId}
                  onTraceEvidence={onTraceEvidence}
                  onDispatchTask={onDispatchTask}
                />
              ))}
            </Space>
          </section>

          {view.compliance ? (
            <section className={styles.scorecardSection}>
              <SectionHeader
                title="合规审核"
                hint="独立维度，不计入综合评分；high_risk 会强制把整剧决策降为「不建议立项」"
              />
              <div style={{ marginTop: 8 }}>
                <ComplianceCard
                  compliance={view.compliance}
                  evidenceMap={evidenceMap}
                  activeEvidenceId={activeEvidenceId}
                  onTraceEvidence={onTraceEvidence}
                  onDispatchTask={onDispatchTask}
                />
              </div>
            </section>
          ) : null}
          {view.rewrite_seeds.length > 0 ? (
            <section className={styles.seedsSection}>
              <SectionHeader
                title="最值得改写"
                hint="低分 / 高风险候选，由 Agent 实时跑改写"
              />
              <Space direction="vertical" size={8} style={{ width: '100%', marginTop: 8 }}>
                {view.rewrite_seeds.map((seed) => (
                  <RewriteSeedCard
                    key={`${seed.scene_id}:${seed.dimension}`}
                    seed={seed}
                    evidenceMap={evidenceMap}
                    taskStatus={view.task_status}
                    activeEvidenceId={activeEvidenceId}
                    onTraceEvidence={onTraceEvidence}
                    onDispatchTask={onDispatchTask}
                  />
                ))}
              </Space>
            </section>
          ) : null}

          {view.risk_flags?.length ? (
            <section className={styles.riskSection}>
              <SectionHeader title="审核风险" hint="需要重点确认的表达风险" />
              <Space wrap size={4} style={{ width: '100%', marginTop: 8 }}>
                {view.risk_flags.map((f) => (
                  <Tag key={f} color="red">{humanizeReportText(f)}</Tag>
                ))}
              </Space>
            </section>
          ) : null}
        </>
      ) : null}

      {/* 底部：清除高亮快捷入口（有 active 时才显示） */}
      {activeEvidenceId ? (
        <Button
          type="text"
          size="small"
          onClick={onClearTrace}
          className={styles.clearTraceBtn}
          block
        >
          清除当前溯源高亮
        </Button>
      ) : null}
    </div>
  )
}

// ============================================================
// 子组件
// ============================================================

function SectionHeader({
  title,
  hint,
}: {
  title: React.ReactNode
  hint?: string
}) {
  return (
    <div className={styles.sectionHeader}>
      <Text strong className={styles.sectionTitle}>
        {title}
      </Text>
      {hint ? (
        <Text type="secondary" className={styles.sectionHint}>
          {hint}
        </Text>
      ) : null}
    </div>
  )
}

function CoverageCardSection({
  coverage,
  evidenceBySceneId,
  activeEvidenceId,
  onTraceEvidence,
}: {
  coverage: NonNullable<ScriptViewResponseDTO['coverage_card']>
  evidenceBySceneId: Map<string, EvidenceRefDTO>
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
}) {
  const info = DECISION_LABEL[coverage.recommendation] || { text: coverage.recommendation, color: 'default' }
  const renderPoint = (point: { title: string; detail: string; anchor_scene_id?: string | null }, tone: 'good' | 'risk') => {
    const evi = point.anchor_scene_id ? evidenceBySceneId.get(point.anchor_scene_id) : undefined
    const active = !!(evi && activeEvidenceId === evi.id)
    return (
      <button
        key={`${tone}:${point.title}:${point.anchor_scene_id || ''}`}
        type="button"
        className={`${styles.actItem} ${active ? styles.actItemActive : ''}`}
        onClick={() => {
          if (!evi) return
          onTraceEvidence({
            evidenceRefId: evi.id,
            sceneId: evi.scene_id,
            startLine: evi.start_line ?? null,
            endLine: evi.end_line ?? null,
          })
        }}
      >
        <Tag color={tone === 'good' ? 'green' : 'orange'}>{point.title}</Tag>
        <span>{humanizeReportText(point.detail)}</span>
      </button>
    )
  }
  return (
    <section className={styles.summarySection}>
      <SectionHeader title="30 秒判断" hint="先判断值不值得继续读" />
      <div className={styles.headlineRow}>
        <Tag color={info.color} className={styles.decisionTag}>{info.text}</Tag>
        {coverage.genre?.slice(0, 3).map((g) => <Tag key={g}>{g}</Tag>)}
      </div>
      <Paragraph className={styles.summaryText}>{humanizeReportText(coverage.logline)}</Paragraph>
      {coverage.core_value ? (
        <Paragraph className={styles.oneLineReason}>核心价值：{humanizeReportText(coverage.core_value)}</Paragraph>
      ) : null}
      <div className={styles.corePlotList}>
        {coverage.strengths?.slice(0, 3).map((p) => renderPoint(p, 'good'))}
        {coverage.concerns?.slice(0, 3).map((p) => renderPoint(p, 'risk'))}
      </div>
    </section>
  )
}

function BeatTimelineSection({
  acts,
  evidenceBySceneId,
  activeEvidenceId,
  onTraceEvidence,
}: {
  acts: BeatActDTO[]
  evidenceBySceneId: Map<string, EvidenceRefDTO>
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
}) {
  return (
    <section className={styles.corePlotSection}>
      <SectionHeader title="故事骨架" hint="三幕结构 + 关键节拍；点击节拍跳原文" />
      <div className={styles.corePlotList}>
        {acts.map((act) => (
          <div key={act.act} className={styles.actGroup}>
            <Tag color="purple">第 {act.act} 幕 · {act.title}</Tag>
            {act.beats.map((beat) => {
              const evi = evidenceBySceneId.get(beat.anchor_scene_id)
              const active = !!(evi && activeEvidenceId === evi.id)
              return (
                <button
                  key={`${act.act}:${beat.type}:${beat.anchor_scene_id}`}
                  type="button"
                  className={`${styles.actItem} ${active ? styles.actItemActive : ''}`}
                  onClick={() => {
                    if (!evi) return
                    onTraceEvidence({
                      evidenceRefId: evi.id,
                      sceneId: evi.scene_id,
                      startLine: evi.start_line ?? null,
                      endLine: evi.end_line ?? null,
                    })
                  }}
                >
                  <Tag color="geekblue">{beat.type}</Tag>
                  <span>{humanizeReportText(beat.summary)}</span>
                </button>
              )
            })}
          </div>
        ))}
      </div>
    </section>
  )
}

function PacingCurveSection({
  points,
}: {
  points: NonNullable<ScriptViewResponseDTO['pacing_curve']>
}) {
  const maxEvent = Math.max(1, ...points.map((p) => p.event_count || 0))
  return (
    <section className={styles.highlightsSection}>
      <SectionHeader title="节奏曲线" hint="每集事件密度 + 情绪起伏，帮助判断哪里拖、哪里抓人" />
      <div className={styles.highlightItems}>
        {points.slice(0, 30).map((p) => (
          <div key={p.episode_no} className={styles.highlightRow}>
            <div className={styles.highlightRowHead}>
              <span className={styles.highlightLocator}>第 {p.episode_no} 集</span>
              <Tag color={p.event_count >= maxEvent * 0.7 ? 'red' : p.event_count > 0 ? 'gold' : 'default'}>
                事件 {p.event_count}
              </Tag>
            </div>
            <Progress percent={Math.round((p.event_count / maxEvent) * 100)} showInfo={false} size="small" />
          </div>
        ))}
      </div>
    </section>
  )
}

function CharacterGraphSection({
  nodes,
  edges,
  evidenceBySceneId,
  activeEvidenceId,
  onTraceEvidence,
}: {
  nodes: CharacterGraphNodeDTO[]
  edges: CharacterGraphEdgeDTO[]
  evidenceBySceneId: Map<string, EvidenceRefDTO>
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
}) {
  const [fullscreenOpen, setFullscreenOpen] = useState(false)

  const nodeById = useMemo(() => {
    const m = new Map<string, CharacterGraphNodeDTO>()
    for (const n of nodes) m.set(n.id, n)
    return m
  }, [nodes])

  const traceNode = useCallback((node: CharacterGraphNodeDTO) => {
    const evi = node.first_scene_id ? evidenceBySceneId.get(node.first_scene_id) : undefined
    if (!evi) return
    onTraceEvidence({
      evidenceRefId: evi.id,
      sceneId: evi.scene_id,
      startLine: evi.start_line ?? null,
      endLine: evi.end_line ?? null,
    })
  }, [evidenceBySceneId, onTraceEvidence])

  // 角色 → 与该角色相关的关系列表（用于角色卡片下方的「与谁、什么关系」清单）
  const relationsByNodeId = useMemo(() => {
    const m = new Map<string, Array<{ otherId: string; otherName: string; type: string; polarity?: string }>>()
    for (const node of nodes) m.set(node.id, [])
    for (const edge of edges) {
      const src = nodeById.get(edge.source_id)
      const dst = nodeById.get(edge.target_id)
      if (!src || !dst) continue
      m.get(src.id)?.push({ otherId: dst.id, otherName: dst.name, type: edge.type, polarity: edge.polarity })
      m.get(dst.id)?.push({ otherId: src.id, otherName: src.name, type: edge.type, polarity: edge.polarity })
    }
    return m
  }, [edges, nodes, nodeById])

  // 头部「主要关系」清单：只看权重 top N，让用户在 5 秒内知道谁跟谁是什么关系
  const topRelations = useMemo(() => {
    return [...edges]
      .sort((a, b) => (b.weight || 0) - (a.weight || 0))
      .slice(0, 8)
      .map((edge) => {
        const src = nodeById.get(edge.source_id)
        const dst = nodeById.get(edge.target_id)
        if (!src || !dst) return null
        return { src, dst, type: edge.type, polarity: edge.polarity, weight: edge.weight }
      })
      .filter((x): x is NonNullable<typeof x> => Boolean(x))
  }, [edges, nodeById])

  const isolatedCharacters = useMemo(() => listIsolatedCharacters(nodes, edges), [nodes, edges])

  return (
    <section className={styles.charactersSection}>
      <SectionHeader
        title="人物关系图"
        hint={nodes.length
          ? '关系列表已按重要性排序；图谱仅作缩略，点「全屏 3D」展开查看完整网络'
          : '尚未抽取到人物'}
      />

      {/* 内联只放一张紧凑预览缩略，避免被右栏宽度卡死；真正的大图在 Modal 里 */}
      <div className={styles.characterGraphPreview}>
        {nodes.length === 0 ? (
          <div className={styles.characterGraphEmpty}>没有可展示的人物</div>
        ) : (
          <CharacterGraph2DPreview
            nodes={nodes}
            edges={edges}
            activeEvidenceId={activeEvidenceId}
            evidenceBySceneId={evidenceBySceneId}
            onNodeClick={traceNode}
          />
        )}
        {nodes.length > 0 && (
          <Button
            type="primary"
            size="small"
            icon={<ExpandOutlined />}
            className={styles.characterGraphExpandBtn}
            onClick={() => setFullscreenOpen(true)}
          >
            展开大图
          </Button>
        )}
      </div>
      {isolatedCharacters.length > 0 && (
        <div className={styles.isolatedHint}>
          未抽到明显关系的角色：{isolatedCharacters.map((n) => n.name).join('、')}
        </div>
      )}

      <div className={styles.graphLegend}>
        <span><strong>身份</strong></span>
        {(['protagonist', 'antagonist', 'support', 'minor'] as const).map((role) => (
          <span key={role}><i style={{ background: CHARACTER_ROLE_COLOR[role] }} />{CHARACTER_ROLE_LABEL[role]}</span>
        ))}
        <span style={{ marginLeft: 8 }}><strong>关系极性</strong></span>
        {(['positive', 'negative', 'mixed'] as const).map((polarity) => (
          <span key={polarity}><i style={{ background: relationColor(polarity) }} />{polarityMeta(polarity).shortLabel}</span>
        ))}
      </div>

      <CharacterGraphFullscreenModal
        open={fullscreenOpen}
        onClose={() => setFullscreenOpen(false)}
        nodes={nodes}
        edges={edges}
        onNodeClick={traceNode}
      />

      {topRelations.length > 0 && (
        <div className={styles.relationListSection}>
          <h4>主要关系（按共现强度 Top {topRelations.length}）</h4>
          <div className={styles.relationList}>
            {topRelations.map((r) => (
              <div key={`${r.src.id}-${r.dst.id}-${r.type}`} className={styles.relationRow}>
                <span className={styles.relationParty}>{r.src.name}</span>
                <Tag color={polarityTagColor(r.polarity)} className={styles.relationTypeTag}>
                  {relationLabel(r.type)}
                </Tag>
                <span className={styles.relationParty}>{r.dst.name}</span>
                <span
                  className={styles.relationPolarityDot}
                  style={{ background: relationColor(r.polarity) }}
                  title={polarityLabel(r.polarity)}
                />
              </div>
            ))}
          </div>
        </div>
      )}

      <div className={styles.characterList}>
        {nodes.map((node) => {
          const evi = node.first_scene_id ? evidenceBySceneId.get(node.first_scene_id) : undefined
          const active = !!(evi && activeEvidenceId === evi.id)
          const rels = (relationsByNodeId.get(node.id) || []).slice(0, 4)
          return (
            <button
              key={node.id}
              type="button"
              className={`${styles.characterCard} ${active ? styles.characterAnchorChipActive : ''}`}
              onClick={() => {
                if (!evi) return
                onTraceEvidence({
                  evidenceRefId: evi.id,
                  sceneId: evi.scene_id,
                  startLine: evi.start_line ?? null,
                  endLine: evi.end_line ?? null,
                })
              }}
            >
              <div className={styles.characterHeader}>
                <Text strong>{node.name}</Text>
                <Tag color={node.role === 'protagonist' ? 'magenta' : node.role === 'antagonist' ? 'red' : 'blue'}>
                  {characterRoleLabel(node.role)}
                </Tag>
              </div>
              {node.motivation ? <CharacterField label="动机" text={humanizeReportText(node.motivation)} /> : null}
              {node.goal ? <CharacterField label="目标" text={humanizeReportText(node.goal)} /> : null}
              {node.obstacle ? <CharacterField label="阻碍" text={humanizeReportText(node.obstacle)} /> : null}
              {rels.length > 0 && (
                <div className={styles.relationCharacterRelations}>
                  {rels.map((r, idx) => (
                    <div key={`${r.otherId}-${r.type}-${idx}`} className={styles.relationCharacterRelationRow}>
                      <span
                        className={styles.relationPolarityDot}
                        style={{ background: relationColor(r.polarity) }}
                      />
                      <span>与</span>
                      <span className={styles.relationParty}>{r.otherName}</span>
                      <Tag color={polarityTagColor(r.polarity)} className={styles.relationTypeTag}>
                        {relationLabel(r.type)}
                      </Tag>
                    </div>
                  ))}
                </div>
              )}
            </button>
          )
        })}
      </div>
    </section>
  )
}

// 关系极性采用 signed social network 的成熟约定：
// 绿色实线=正向支持，红色虚线=负向冲突，紫色点线=复杂/暧昧/利益拉扯。
// mixed 不是“不确定”，而是同时存在合作与冲突证据的 ambivalent tie。
const RELATION_POLARITY_META: Record<string, {
  label: string
  shortLabel: string
  color: string
  tagColor: string
  lineType: 'solid' | 'dashed' | 'dotted'
  description: string
}> = {
  positive: {
    label: '正向（合作/支持）',
    shortLabel: '正向',
    color: '#2F855A',
    tagColor: 'green',
    lineType: 'solid',
    description: '合作、保护、信任、利益一致或稳定支持',
  },
  negative: {
    label: '负向（冲突/伤害）',
    shortLabel: '负向',
    color: '#C0392B',
    tagColor: 'red',
    lineType: 'dashed',
    description: '敌对、压制、背叛、威胁、竞争或目标冲突',
  },
  mixed: {
    label: '复杂（爱恨/利益拉扯）',
    shortLabel: '复杂',
    color: '#7C5AA6',
    tagColor: 'purple',
    lineType: 'dotted',
    description: '同时存在合作与冲突，如亲密但对立、同盟但互相利用',
  },
}

function polarityMeta(polarity?: string) {
  return RELATION_POLARITY_META[polarity || 'mixed'] || RELATION_POLARITY_META.mixed
}

function polarityTagColor(polarity?: string): string {
  return polarityMeta(polarity).tagColor
}

function polarityLabel(polarity?: string): string {
  return polarityMeta(polarity).label
}

// 角色 4 色板：直接套 Tableau 10 / 影视惯例语义色，再降饱和度对齐莫兰迪基调
// 选色逻辑（参考 D3 schemeTableau10、ColorBrewer Set1、Marvel/DC 漫画色系）：
//   蓝=主角（Hero blue：超人蓝、海军蓝）
//   红=反派（Villain red：用户最强烈的敌对色直觉）
//   金=关键配角（Sidekick gold：Disney 配角高光色）
//   灰青=配角（背景人物，低饱和不抢戏）
// 色相 207°/8°/38°/180°，红蓝对位 200°，色盲安全
const CHARACTER_ROLE_COLOR: Record<string, string> = {
  protagonist: '#3E78A1',  // hsl(207,45%,44%) — Hero blue，正派直觉
  antagonist:  '#C0594A',  // hsl(8,50%,52%)   — Villain red，敌对直觉
  support:     '#D4A04C',  // hsl(38,60%,56%)  — Sidekick gold，金黄高光
  minor:       '#7E9C9C',  // hsl(180,12%,55%) — 中性灰青，背景人物
}
const CHARACTER_ROLE_FALLBACK = '#8EA7B8'

const CHARACTER_ROLE_LABEL: Record<string, string> = {
  protagonist: '主角',
  antagonist: '反派',
  support: '关键配角',
  minor: '配角',
}

function characterRoleLabel(role: string) {
  return CHARACTER_ROLE_LABEL[role] || '人物'
}

function characterRoleColor(role: string) {
  return CHARACTER_ROLE_COLOR[role] || CHARACTER_ROLE_FALLBACK
}

// ============================================================
// 人物关系图：内联 2D 缩略（react-force-graph）+ 全屏 ECharts series-graph
// ECharts 默认环形布局，节点均匀分布在圆环上、边带中文关系标签
// ============================================================

type CharGraphData = {
  nodes: Array<CharacterGraphNodeDTO & { val: number; color: string }>
  links: Array<{
    source: string
    target: string
    type?: string
    polarity?: string
    weight: number
  }>
}

function buildCharGraphData(
  nodes: CharacterGraphNodeDTO[],
  edges: CharacterGraphEdgeDTO[],
): CharGraphData {
  const allIds = new Set(nodes.map((n) => n.id))
  // 先过出有效边
  const validEdges = edges.filter((e) => allIds.has(e.source_id) && allIds.has(e.target_id))
  // 只保留有连接的节点 —— 孤立点对力图布局只是噪声（飘在角落抢视觉），
  // 角色卡列表仍由 nodes 全量驱动，没有信息丢失。
  const connected = new Set<string>()
  for (const e of validEdges) {
    connected.add(e.source_id)
    connected.add(e.target_id)
  }
  return {
    nodes: nodes
      .filter((n) => connected.has(n.id))
      .map((node) => ({
        ...node,
        val: Math.max(8, Math.min(22, Math.sqrt(node.appearance_count || 1) * 5)),
        color: characterRoleColor(node.role),
      })),
    links: validEdges.map((e) => ({
      source: e.source_id,
      target: e.target_id,
      type: e.type,
      polarity: e.polarity,
      weight: e.weight,
    })),
  }
}

// 计算孤立的角色（没有任何关系边）—— 关系图里不画，但下方提示用
function listIsolatedCharacters(
  nodes: CharacterGraphNodeDTO[],
  edges: CharacterGraphEdgeDTO[],
): CharacterGraphNodeDTO[] {
  const connected = new Set<string>()
  for (const e of edges) {
    connected.add(e.source_id)
    connected.add(e.target_id)
  }
  return nodes.filter((n) => !connected.has(n.id))
}

// 缩略预览：固定 240px 高度、不显示边标签（信息密度让位给关系列表）
function CharacterGraph2DPreview({
  nodes,
  edges,
  activeEvidenceId,
  evidenceBySceneId,
  onNodeClick,
}: {
  nodes: CharacterGraphNodeDTO[]
  edges: CharacterGraphEdgeDTO[]
  activeEvidenceId: string | null
  evidenceBySceneId: Map<string, EvidenceRefDTO>
  onNodeClick: (node: CharacterGraphNodeDTO) => void
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const fgRef = useRef<unknown>(null)
  const [size, setSize] = useState({ w: 360, h: 240 })

  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const update = () => setSize({
      w: Math.max(280, Math.floor(el.clientWidth || 360)),
      h: 240,
    })
    update()
    const obs = new ResizeObserver(update)
    obs.observe(el)
    return () => obs.disconnect()
  }, [])

  const data = useMemo(() => buildCharGraphData(nodes, edges), [nodes, edges])

  // 关键：把 link.strength 调小、加 collide force，避免家族高权重边把人物吸成黑洞
  useEffect(() => {
    type ForceFn = { strength?: (v: number | ((l: unknown) => number)) => unknown; distance?: (v: number) => unknown }
    type FgHandle = {
      d3Force?: (name: string, force?: unknown) => ForceFn | undefined
      d3ReheatSimulation?: () => void
    }
    const fg = fgRef.current as FgHandle | null
    if (!fg?.d3Force) return
    fg.d3Force('charge')?.strength?.(-260)
    fg.d3Force('link')?.distance?.(70)
    fg.d3Force('link')?.strength?.(0.18)
    fg.d3Force('collide', forceCollide2D((n: unknown) => ((n as { val?: number }).val || 10) + 4))
    fg.d3ReheatSimulation?.()
  }, [data])

  const handleStop = useCallback(() => {
    type FgHandle = { zoomToFit?: (ms?: number, padding?: number) => void }
    const fg = fgRef.current as FgHandle | null
    fg?.zoomToFit?.(400, 25)
  }, [])

  const activeNodeIds = useMemo(() => {
    const out = new Set<string>()
    for (const n of nodes) {
      const evi = n.first_scene_id ? evidenceBySceneId.get(n.first_scene_id) : undefined
      if (evi && evi.id === activeEvidenceId) out.add(n.id)
    }
    return out
  }, [nodes, evidenceBySceneId, activeEvidenceId])

  return (
    <div ref={wrapRef} className={styles.characterGraphCanvas}>
      <ForceGraph2D
        ref={fgRef as unknown as React.MutableRefObject<undefined>}
        graphData={data}
        width={size.w}
        height={size.h}
        backgroundColor="rgba(255, 252, 248, 0)"
        cooldownTicks={70}
        d3VelocityDecay={0.32}
        onEngineStop={handleStop}
        nodeLabel={(node) => {
          const n = node as CharacterGraphNodeDTO
          return `${n.name} · ${characterRoleLabel(n.role)}`
        }}
        linkColor={(l) => relationColor((l as { polarity?: string }).polarity)}
        linkWidth={(l) => Math.max(1.2, ((l as { weight?: number }).weight || 0.2) * 3)}
        // 关键：之前 hover 不弹关系名是因为 linkLabel 没传；ForceGraph2D 的
        // 默认 hover tooltip 用的是 link.label / linkLabel 回调
        linkLabel={(l) => {
          const link = l as { type?: string; polarity?: string; weight?: number }
          const type = relationLabel(link.type)
          const pol = polarityMeta(link.polarity).label
          return `${type} · ${pol} · 共现强度 ${(link.weight || 0).toFixed(2)}`
        }}
        linkHoverPrecision={6}
        onNodeClick={(n) => onNodeClick(n as CharacterGraphNodeDTO)}
        nodeCanvasObject={(node, ctx, globalScale) => {
          const n = node as CharacterGraphNodeDTO & { x?: number; y?: number; val?: number; color?: string }
          const x = n.x || 0
          const y = n.y || 0
          const screenR = Math.max(7, Math.min(14, (n.val || 10) * 0.7))
          const radius = screenR / globalScale
          const active = activeNodeIds.has(n.id)
          ctx.beginPath()
          ctx.arc(x, y, radius + (active ? 2 / globalScale : 0), 0, 2 * Math.PI)
          ctx.fillStyle = active ? '#E07A8C' : (n.color || '#8EA7B8')
          ctx.fill()
          ctx.lineWidth = (active ? 2 : 1.2) / globalScale
          ctx.strokeStyle = '#FFFFFF'
          ctx.stroke()

          const fontSize = 11 / globalScale
          ctx.font = `${fontSize}px "PingFang SC", "Microsoft YaHei", sans-serif`
          ctx.textAlign = 'center'
          ctx.textBaseline = 'middle'
          ctx.fillStyle = '#3F3835'
          ctx.fillText(n.name, x, y + radius + fontSize * 0.85)
        }}
      />
    </div>
  )
}

// 全屏 Modal：用 ECharts series-graph 画大图。
// - 默认 circular 布局：节点均匀放圆环上，算法保证不重叠不塌陷
// - 可切换 force 布局，参数已调好让强连边别拉黑洞
// - 边标签默认显示关系类型，悬停弹完整 tooltip
type FullscreenLayout = 'circular' | 'force'

function CharacterGraphFullscreenModal({
  open,
  onClose,
  nodes,
  edges,
  onNodeClick,
}: {
  open: boolean
  onClose: () => void
  nodes: CharacterGraphNodeDTO[]
  edges: CharacterGraphEdgeDTO[]
  onNodeClick: (node: CharacterGraphNodeDTO) => void
}) {
  const [layout, setLayout] = useState<FullscreenLayout>('circular')
  // antd v5 .ant-modal-content 自带 24px 左右 padding，按 92vw 直接算宽度会让
  // canvas 比可视区域宽，圆环看上去偏左。改成实测容器 clientWidth，高度走 viewport。
  const chartRef = useRef<ReactECharts | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [size, setSize] = useState<{ w: number; h: number } | null>(null)

  const isolated = useMemo(() => listIsolatedCharacters(nodes, edges), [nodes, edges])
  const graphIds = useMemo(() => {
    const allIds = new Set(nodes.map((n) => n.id))
    const validEdges = edges.filter((e) => allIds.has(e.source_id) && allIds.has(e.target_id))
    const connected = new Set<string>()
    for (const e of validEdges) {
      connected.add(e.source_id)
      connected.add(e.target_id)
    }
    return { nodes: nodes.filter((n) => connected.has(n.id)), edges: validEdges }
  }, [nodes, edges])

  const option = useMemo(() => buildEChartsGraphOption(graphIds.nodes, graphIds.edges, layout), [graphIds, layout])

  const onEvents = useMemo(() => ({
    click: (params: { dataType?: string; data?: { id?: string } }) => {
      if (params?.dataType !== 'node') return
      const id = params?.data?.id
      const node = nodes.find((n) => n.id === id)
      if (node) onNodeClick(node)
    },
  }), [nodes, onNodeClick])

  // open 时实测容器宽度 + 视口高度算 size。
  // 关键：antd Modal 走 React Portal，effect 首次跑时 ref 可能未挂载，所以
  // 用 rAF 自旋直到测到合法 width 再挂 ResizeObserver 持续跟踪。
  useEffect(() => {
    if (!open) {
      setSize(null)
      return
    }
    let cancelled = false
    let rafId = 0
    let obs: ResizeObserver | null = null

    const apply = () => {
      const el = containerRef.current
      if (!el) return false
      const w = el.clientWidth
      if (w < 100) return false
      const h = Math.max(420, Math.floor(window.innerHeight * 0.85) - 70)
      setSize((prev) => (prev && prev.w === w && prev.h === h ? prev : { w, h }))
      return true
    }

    const spin = () => {
      if (cancelled) return
      if (apply()) {
        const el = containerRef.current
        if (el) {
          obs = new ResizeObserver(apply)
          obs.observe(el)
        }
      } else {
        rafId = requestAnimationFrame(spin)
      }
    }

    spin()
    const onResize = () => apply()
    window.addEventListener('resize', onResize)

    return () => {
      cancelled = true
      cancelAnimationFrame(rafId)
      obs?.disconnect()
      window.removeEventListener('resize', onResize)
    }
  }, [open])

  useEffect(() => {
    if (!size) return
    chartRef.current?.getEchartsInstance?.()?.resize()
  }, [layout, size])

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width="92vw"
      style={{ top: 24 }}
      styles={{ body: { padding: 0, background: '#FFFCF8' } }}
      destroyOnClose
      title={
        <Space size={14} wrap>
          <span style={{ color: '#3F3835' }}>人物关系图</span>
          <span className={styles.characterGraphLayoutSwitchHint}>布局：</span>
          <Segmented
            size="middle"
            value={layout}
            onChange={(v) => setLayout(v as FullscreenLayout)}
            className={styles.characterGraphLayoutSegmented}
            options={[
              { label: '环形（无重叠）', value: 'circular' },
              { label: '力导向（结构感）', value: 'force' },
            ]}
          />
          <span style={{ color: '#8F8178', fontSize: 12, fontWeight: 'normal' }}>
            节点大小 = 出场频次 · 连线粗细 = 共现强度
          </span>
        </Space>
      }
    >
      <div ref={containerRef} className={styles.characterGraphEchartsContainer}>
        {graphIds.nodes.length > 0 && <CharacterGraphLegendBar />}
        {graphIds.nodes.length === 0 ? (
          <div className={styles.characterGraph3DEmpty}>没有提取到角色之间的关系</div>
        ) : size ? (
          <ReactECharts
            ref={chartRef}
            echarts={echarts}
            option={option}
            onEvents={onEvents}
            notMerge
            lazyUpdate
            style={{ width: '100%', height: `${Math.max(360, size.h - 48)}px`, display: 'block' }}
            opts={{ renderer: 'canvas', width: size.w, height: Math.max(360, size.h - 48) }}
          />
        ) : (
          <div className={styles.characterGraph3DEmpty}>正在准备关系图…</div>
        )}
        {isolated.length > 0 && (
          <div className={styles.characterGraphEchartsIsolated}>
            未抽到关系：{isolated.map((n) => n.name).join('、')}
          </div>
        )}
      </div>
    </Modal>
  )
}

// 顶部图例 bar：节点角色色块 + 边极性线型说明。HTML 渲染避免挤占 ECharts 画布
function CharacterGraphLegendBar() {
  return (
    <div className={styles.characterGraphLegendBar}>
      <div className={styles.characterGraphLegendGroup}>
        <span className={styles.characterGraphLegendGroupLabel}>节点身份</span>
        {(['protagonist', 'antagonist', 'support', 'minor'] as const).map((role) => (
          <span key={role} className={styles.characterGraphLegendItem}>
            <span
              className={styles.characterGraphLegendDot}
              style={{ background: CHARACTER_ROLE_COLOR[role] }}
            />
            {CHARACTER_ROLE_LABEL[role]}
          </span>
        ))}
      </div>
      <span className={styles.characterGraphLegendDivider} />
      <div className={styles.characterGraphLegendGroup}>
        <span className={styles.characterGraphLegendGroupLabel}>关系极性</span>
        <span className={styles.characterGraphLegendItem}>
          <span className={styles.characterGraphLegendLineSolid} style={{ background: polarityMeta('positive').color }} />
          {polarityMeta('positive').label}
        </span>
        <span className={styles.characterGraphLegendItem}>
          <span className={styles.characterGraphLegendLineDashed} style={{ borderColor: polarityMeta('negative').color }} />
          {polarityMeta('negative').label}
        </span>
        <span className={styles.characterGraphLegendItem}>
          <span className={styles.characterGraphLegendLineDotted} style={{ borderColor: polarityMeta('mixed').color }} />
          {polarityMeta('mixed').label}
        </span>
      </div>
    </div>
  )
}

function buildEChartsGraphOption(
  nodes: CharacterGraphNodeDTO[],
  edges: CharacterGraphEdgeDTO[],
  layout: FullscreenLayout,
): Record<string, unknown> {
  // ECharts categories 颜色来源于共享常量 CHARACTER_ROLE_COLOR，避免与角色卡 Tag 脱钩
  const ROLE_CATEGORIES = [
    { name: CHARACTER_ROLE_LABEL.protagonist, itemStyle: { color: CHARACTER_ROLE_COLOR.protagonist } },
    { name: CHARACTER_ROLE_LABEL.antagonist, itemStyle: { color: CHARACTER_ROLE_COLOR.antagonist } },
    { name: CHARACTER_ROLE_LABEL.support, itemStyle: { color: CHARACTER_ROLE_COLOR.support } },
    { name: CHARACTER_ROLE_LABEL.minor, itemStyle: { color: CHARACTER_ROLE_COLOR.minor } },
  ]
  const roleToCategory = (role?: string) => {
    const map: Record<string, number> = { protagonist: 0, antagonist: 1, support: 2, minor: 3 }
    return map[role || ''] ?? 3
  }

  const ePoints = nodes.map((n) => ({
    id: n.id,
    name: n.name,
    category: roleToCategory(n.role),
    symbolSize: Math.max(28, Math.min(78, Math.sqrt(n.appearance_count || 1) * 14)),
    value: n.appearance_count || 0,
    itemStyle: { borderColor: '#FFFFFF', borderWidth: 2 },
    label: {
      show: true,
      position: 'right',
      fontSize: 13,
      color: '#3F3835',
      fontWeight: 500,
    },
    _role: characterRoleLabel(n.role),
    _motivation: n.motivation || '',
    _goal: n.goal || '',
    _obstacle: n.obstacle || '',
  }))
  // 力导向布局下，永久挂边标签会让 8-10 节点的中心区互相遮挡；改为只在
  // circular 布局展示边名（圆环外圈空间够），force 布局走 emphasis 悬停弹标签
  const eLinks = edges.map((e) => ({
    source: e.source_id,
    target: e.target_id,
    value: e.weight,
    lineStyle: {
      color: relationColor(e.polarity),
      width: Math.max(1.4, (e.weight || 0.2) * 5),
      type: polarityMeta(e.polarity).lineType,
      opacity: 0.85,
      curveness: layout === 'circular' ? 0.18 : 0,
    },
    label: layout === 'circular'
      ? {
          show: true,
          formatter: relationLabel(e.type),
          fontSize: 12,
          color: '#5A4F47',
          backgroundColor: 'rgba(255, 252, 248, 0.92)',
          padding: [3, 6],
          borderRadius: 3,
        }
      : { show: false },
    emphasis: layout === 'force' ? {
      label: {
        show: true,
        formatter: relationLabel(e.type),
        fontSize: 12,
        color: '#5A4F47',
        backgroundColor: 'rgba(255, 252, 248, 0.95)',
        padding: [3, 6],
        borderRadius: 3,
      },
    } : undefined,
    _type: relationLabel(e.type),
    _polarity: polarityMeta(e.polarity).label,
    _polarityDescription: polarityMeta(e.polarity).description,
  }))

  return {
    backgroundColor: '#FFFCF8',
    tooltip: {
      trigger: 'item',
      backgroundColor: '#2C333D',
      borderColor: '#2C333D',
      textStyle: { color: '#F2E8DA', fontSize: 12 },
      formatter: (params: {
        dataType?: string
        data?: Record<string, unknown>
      }) => {
        if (params.dataType === 'node') {
          const d = params.data as { name?: string; value?: number; _role?: string; _motivation?: string; _goal?: string; _obstacle?: string }
          const lines = [
            `<strong style="color:#FFC9A3">${d.name}</strong> · ${d._role || ''}`,
            `出场 ${d.value || 0} 场`,
            d._motivation ? `动机：${d._motivation}` : '',
            d._goal ? `目标：${d._goal}` : '',
            d._obstacle ? `阻碍：${d._obstacle}` : '',
          ].filter(Boolean)
          return lines.join('<br/>')
        }
        if (params.dataType === 'edge') {
          const d = params.data as { _type?: string; _polarity?: string; _polarityDescription?: string; value?: number }
          return `<strong>${d._type || '关系'}</strong> · ${d._polarity || ''}<br/>${d._polarityDescription || ''}<br/>共现强度 ${(d.value || 0).toFixed(2)}`
        }
        return ''
      },
    },
    animationDurationUpdate: 600,
    animationEasingUpdate: 'cubicInOut',
    // 注意：legend / graphic 不放在 ECharts 内（会压缩画布让环形挤边），
    // 改在 modal 顶部 HTML bar 显示，画布留给 graph
    series: [
      {
        type: 'graph',
        layout,
        categories: ROLE_CATEGORIES,
        // 力导向：ECharts force layout 不通过 center 居中，靠 gravity 把节点拉到画布中点。
        // gravity 太小 → 节点群偏向某一边；太大 → 全部挤在中心。0.1 是经验甜区
        force: layout === 'force' ? {
          initLayout: 'circular',
          repulsion: 1600,
          edgeLength: [240, 380],
          gravity: 0.1,
          friction: 0.5,
          layoutAnimation: true,
        } : undefined,
        circular: layout === 'circular' ? { rotateLabel: false } : undefined,
        center: ['50%', '50%'],
        roam: true,
        draggable: layout === 'force',
        focusNodeAdjacency: true,
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: [0, 8],
        emphasis: {
          focus: 'adjacency',
          lineStyle: { width: 4 },
          itemStyle: { borderColor: '#E07A8C', borderWidth: 3 },
        },
        data: ePoints,
        edges: eLinks,
        lineStyle: { opacity: 0.85 },
      },
    ],
  }
}

function relationColor(polarity?: string) {
  return polarityMeta(polarity).color
}

function relationLabel(type?: string) {
  return ({
    family: '家人',
    romance: '感情',
    rival: '对抗',
    ally: '同盟',
    authority: '权力/上下级',
    deception: '欺骗',
    mentor: '师徒/引导',
  } as Record<string, string>)[type || 'ally'] || '关系'
}

interface DimCardProps {
  item: ScorecardItemDTO
  evidenceMap: Map<string, EvidenceRefDTO>
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
  onDispatchTask: (task: AgentTask) => void
}

function DimCard({
  item,
  evidenceMap,
  activeEvidenceId,
  onTraceEvidence,
  onDispatchTask,
}: DimCardProps) {
  const label = DIMENSION_LABELS[item.dimension] || item.dimension
  const hint = DIMENSION_HINTS[item.dimension]
  const isNoScore = item.score === null || item.score === undefined
  const evidences: EvidenceRefDTO[] = (item.evidence_ref_ids || [])
    .map((rid) => evidenceMap.get(rid))
    .filter((x): x is EvidenceRefDTO => x !== undefined)

  return (
    <div className={styles.dimCard}>
      <div className={styles.dimCardHeader}>
        <span className={styles.dimLabel}>
          {label}
          {hint ? <span className={styles.dimHint}>· {hint}</span> : null}
        </span>
        <Space size={6} className={styles.dimRight}>
          {item.level ? (
            <Tag color={LEVEL_COLOR[item.level] || 'default'} className={styles.dimLevelTag}>
              {LEVEL_LABEL[item.level] || item.level}
            </Tag>
          ) : null}
          <span className={styles.dimScore}>
            {isNoScore ? '—' : `${item.score} / 10`}
          </span>
          <Tooltip title={`让 Agent 解释「${label}」为什么这么打分`}>
            <Button
              type="text"
              size="small"
              icon={<SearchOutlined />}
              className={styles.dimInquiryBtn}
              onClick={() =>
                onDispatchTask({
                  kind: 'dim_inquiry',
                  dimension: item.dimension,
                  current_score: item.score ?? null,
                })
              }
            />
          </Tooltip>
        </Space>
      </div>

      {item.reason ? (
        <Paragraph className={styles.dimReason} ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}>
          {humanizeReportText(item.reason)}
        </Paragraph>
      ) : null}

      {evidences.length > 0 && (
        <div className={styles.evidenceList}>
          {evidences.slice(0, 5).map((ref) => (
            <EvidenceChip
              key={ref.id}
              evidence={ref}
              active={activeEvidenceId === ref.id}
              onTrace={() =>
                onTraceEvidence({
                  evidenceRefId: ref.id,
                  sceneId: ref.scene_id,
                  startLine: ref.start_line ?? null,
                  endLine: ref.end_line ?? null,
                })
              }
            />
          ))}
        </div>
      )}
    </div>
  )
}

// === 合规审核独立卡片（docs/08 §4，与五力分离） ===
interface ComplianceCardProps {
  compliance: ComplianceDTO
  evidenceMap: Map<string, EvidenceRefDTO>
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
  onDispatchTask: (task: AgentTask) => void
}

function ComplianceCard({
  compliance,
  evidenceMap,
  activeEvidenceId,
  onTraceEvidence,
  onDispatchTask,
}: ComplianceCardProps) {
  const label = DIMENSION_LABELS.compliance
  const hint = DIMENSION_HINTS.compliance
  const isNoScore = compliance.score === null || compliance.score === undefined
  const evidences: EvidenceRefDTO[] = (compliance.evidence_ref_ids || [])
    .map((rid) => evidenceMap.get(rid))
    .filter((x): x is EvidenceRefDTO => x !== undefined)

  return (
    <div className={styles.dimCard}>
      <div className={styles.dimCardHeader}>
        <span className={styles.dimLabel}>
          {label}
          {hint ? <span className={styles.dimHint}>· {hint}</span> : null}
        </span>
        <Space size={6} className={styles.dimRight}>
          {compliance.level ? (
            <Tag color={LEVEL_COLOR[compliance.level] || 'default'} className={styles.dimLevelTag}>
              {LEVEL_LABEL[compliance.level] || compliance.level}
            </Tag>
          ) : null}
          <span className={styles.dimScore}>
            {isNoScore ? '—' : `${compliance.score} / 10`}
          </span>
          <Tooltip title="让 Agent 解释合规审核结果">
            <Button
              type="text"
              size="small"
              icon={<SearchOutlined />}
              className={styles.dimInquiryBtn}
              onClick={() =>
                onDispatchTask({
                  kind: 'dim_inquiry',
                  dimension: 'compliance',
                  current_score: compliance.score ?? null,
                })
              }
            />
          </Tooltip>
        </Space>
      </div>

      {compliance.reason ? (
        <Paragraph className={styles.dimReason} ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}>
          {humanizeReportText(compliance.reason)}
        </Paragraph>
      ) : null}

      {evidences.length > 0 && (
        <div className={styles.evidenceList}>
          {evidences.slice(0, 5).map((ref) => (
            <EvidenceChip
              key={ref.id}
              evidence={ref}
              active={activeEvidenceId === ref.id}
              onTrace={() =>
                onTraceEvidence({
                  evidenceRefId: ref.id,
                  sceneId: ref.scene_id,
                  startLine: ref.start_line ?? null,
                  endLine: ref.end_line ?? null,
                })
              }
            />
          ))}
        </div>
      )}
    </div>
  )
}

// === 证据 chip：点击跳原文 + 双向高亮（不派 Agent） ===
function EvidenceChip({
  evidence,
  active,
  onTrace,
}: {
  evidence: EvidenceRefDTO
  active: boolean
  onTrace: () => void
}) {
  const locator = formatSceneLocator(evidence.episode_no, evidence.scene_no, evidence.scene_label)
  const tip = evidence.quote
    ? `${locator}\n"${evidence.quote}"\n\n点击 → 跳原文 + 双向高亮`
    : `${locator} · 点击跳原文`
  return (
    <Tooltip title={<span style={{ whiteSpace: 'pre-wrap' }}>{tip}</span>} placement="top">
      <Tag
        className={`${styles.evidenceChip} ${active ? styles.evidenceChipActive : ''}`}
        color={active ? 'magenta' : 'geekblue'}
        onClick={onTrace}
      >
        {locator || (evidence.scene_id || '').slice(0, 6)}
      </Tag>
    </Tooltip>
  )
}

function KeyScenesSection({
  evidences,
  activeEvidenceId,
  onTraceEvidence,
}: {
  evidences: EvidenceRefDTO[]
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
}) {
  return (
    <section className={styles.mustReadSection}>
      <SectionHeader
        title="关键场景"
        hint={`Top ${evidences.length} · 点击跳原文`}
      />
      <Space direction="vertical" size={6} style={{ width: '100%', marginTop: 8 }}>
        {evidences.map((ref) => (
          <MustReadChip
            key={ref.id}
            evidence={ref}
            active={activeEvidenceId === ref.id}
            onTrace={() =>
              onTraceEvidence({
                evidenceRefId: ref.id,
                sceneId: ref.scene_id,
                startLine: ref.start_line ?? null,
                endLine: ref.end_line ?? null,
              })
            }
          />
        ))}
      </Space>
    </section>
  )
}

// === 关键场景：点击跳原文 + 双向高亮 ===
function MustReadChip({
  evidence,
  active,
  onTrace,
}: {
  evidence: EvidenceRefDTO
  active: boolean
  onTrace: () => void
}) {
  const locator = formatSceneLocator(evidence.episode_no, evidence.scene_no, evidence.scene_label)
  const summary = evidence.scene_summary || evidence.quote || ''
  return (
    <button
      type="button"
      className={`${styles.mustReadItem} ${active ? styles.mustReadItemActive : ''}`}
      onClick={onTrace}
    >
      <div className={styles.mustReadHeader}>
        <Tag color={active ? 'magenta' : 'purple'} className={styles.mustReadSceneTag}>
          {locator || (evidence.scene_id || '').slice(0, 8)}
        </Tag>
      </div>
      {summary ? (
        <div className={styles.mustReadQuote}>{humanizeReportText(summary)}</div>
      ) : null}
    </button>
  )
}

function CharacterField({ label, text }: { label: string; text: string }) {
  return (
    <div className={styles.characterFieldRow}>
      <span className={styles.characterFieldLabel}>{label}</span>
      <span className={styles.characterFieldText}>{text}</span>
    </div>
  )
}

function HighlightsSection({
  highlights,
  activeEvidenceId,
  onTraceEvidence,
  grouped,
  groups,
  hint,
}: {
  highlights: HighlightDTO[]
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
  grouped: boolean
  groups?: HighlightGroup[]
  hint: string
}) {
  if (!highlights.length) return null

  const renderRow = (h: HighlightDTO) => (
    <HighlightRow
      key={h.id}
      hl={h}
      active={activeEvidenceId === h.id}
      onTrace={() =>
        onTraceEvidence({
          evidenceRefId: h.id,
          sceneId: h.scene_id,
          startLine: h.start_line ?? null,
          endLine: h.end_line ?? null,
        })
      }
    />
  )

  return (
    <section className={styles.highlightsSection}>
      <SectionHeader
        title={
          <span>
            <ThunderboltOutlined style={{ marginRight: 4, color: '#E07A8C' }} />
            主要看点
          </span>
        }
        hint={hint}
      />
      <div className={styles.highlightList}>
        {grouped ? (
          (groups || groupHighlights(highlights)).map((group) => (
            <div key={group.type} className={styles.highlightGroup}>
              <Tag
                color={HIGHLIGHT_COLOR[group.type]}
                className={styles.highlightGroupTag}
              >
                {HIGHLIGHT_LABEL[group.type]} · {group.items.length}
              </Tag>
              <div className={styles.highlightItems}>
                {group.items.slice(0, 6).map(renderRow)}
              </div>
            </div>
          ))
        ) : (
          <div className={styles.highlightItems}>{highlights.map(renderRow)}</div>
        )}
      </div>
    </section>
  )
}

// === 主要看点行 ===
function HighlightRow({
  hl,
  active,
  onTrace,
}: {
  hl: HighlightDTO
  active: boolean
  onTrace: () => void
}) {
  const locator = formatSceneLocator(hl.episode_no, hl.scene_no, hl.scene_label)
  return (
    <button
      type="button"
      className={`${styles.highlightRow} ${active ? styles.highlightRowActive : ''}`}
      onClick={onTrace}
      title={hl.evidence ? `"${hl.evidence}"` : undefined}
    >
      <div className={styles.highlightRowHead}>
        <span className={styles.highlightLocator}>{locator || hl.scene_id.slice(0, 6)}</span>
      </div>
      <div className={styles.highlightOneliner}>{humanizeReportText(hl.oneliner)}</div>
    </button>
  )
}

// === 改写候选卡片 ===
function RewriteSeedCard({
  seed,
  evidenceMap,
  taskStatus,
  activeEvidenceId,
  onTraceEvidence,
  onDispatchTask,
}: {
  seed: RewriteSeedDTO
  evidenceMap: Map<string, EvidenceRefDTO>
  taskStatus: Record<string, RewriteTaskStatusDTO>
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
  onDispatchTask: (task: AgentTask) => void
}) {
  const dimLabel = DIMENSION_LABELS[seed.dimension] || seed.dimension
  const evi = evidenceMap.get(seed.evidence_ref_id)
  const status = taskStatus[`${seed.scene_id}:${seed.dimension}`]
  const badge = renderTaskBadge(status)
  const locator = evi
    ? formatSceneLocator(evi.episode_no, evi.scene_no, evi.scene_label)
    : seed.scene_label || seed.scene_id.slice(0, 8)

  return (
    <div className={styles.seedCard}>
      <div className={styles.seedHeader}>
        <Space size={6}>
          <Tag color="gold" className={styles.seedDimTag}>{dimLabel}</Tag>
          <Text className={styles.seedScene}>{locator}</Text>
        </Space>
        {badge}
      </div>
      <div className={styles.seedIssue}>{humanizeReportText(seed.issue || '（暂无 issue）')}</div>
      <div className={styles.seedActions}>
        <Button
          size="small"
          icon={<EditOutlined />}
          type="primary"
          ghost
          onClick={() =>
            onDispatchTask({
              kind: 'rewrite_seed',
              dimension: seed.dimension,
              scene_id: seed.scene_id,
              scene_label: seed.scene_label,
              issue: humanizeReportText(seed.issue),
              evidence_ref_id: seed.evidence_ref_id,
            })
          }
        >
          让 Agent 改写
        </Button>
        {evi ? (
          <Button
            size="small"
            icon={<SearchOutlined />}
            onClick={() =>
              onTraceEvidence({
                evidenceRefId: evi.id,
                sceneId: evi.scene_id,
                startLine: evi.start_line ?? null,
                endLine: evi.end_line ?? null,
              })
            }
            type={activeEvidenceId === evi.id ? 'primary' : 'default'}
            ghost={activeEvidenceId === evi.id}
          >
            先看原文
          </Button>
        ) : null}
      </div>
    </div>
  )
}

function renderTaskBadge(status: RewriteTaskStatusDTO | undefined) {
  if (!status || status.attempts <= 0) {
    return <Tag className={styles.badgeIdle}>未处理</Tag>
  }
  if (status.last_status === 'accepted') {
    return <Tag color="green" className={styles.badgeAccepted}>已采纳改写</Tag>
  }
  if (status.last_status === 'rejected') {
    return <Tag color="orange">上次拒绝，可重试</Tag>
  }
  return <Tag color="blue">已尝试 {status.attempts} 次</Tag>
}

// ============================================================
// 工具函数
// ============================================================

interface HighlightGroup {
  type: HighlightType
  items: HighlightDTO[]
}

const HIGHLIGHT_TYPE_ORDER: HighlightType[] = [
  'hook',
  'reversal',
  'face_slap',
  'identity_reveal',
  'revenge',
  'villain_fall',
  'underdog_rise',
  'cp_progress',
  'scheme_exposed',
  'risk',
]

function humanizeReportText(input: string | null | undefined): string {
  const raw = String(input || '').trim()
  if (!raw) return ''

  // 后端早期评分 reason 含工程指标，不能直接给剧本创作者/审核员看。
  // 这里先做前端兜底清洗；重新诊断后，后端 prompt 也会约束为人话。
  if (/reward\/集数比值|最长连续无reward|无reward/.test(raw)) {
    return '爽点密度偏低，连续多集缺少明确的情绪回报；建议在中前段补强反转、打脸或情绪释放。'
  }
  if (/方差|均值|密度/.test(raw) && /节奏|最长连续|中段/.test(raw)) {
    return '节奏整体较稳，但中段起伏不够明显；如果阅读疲劳，可以增加更清晰的阶段性冲突和阶段回报。'
  }
  if (/setup_count|OOC|铺垫充足|无铺垫/.test(raw)) {
    return raw
      .replace(/setup_count/gi, '铺垫数量')
      .replace(/\bOOC\b/g, '角色行为突兀')
      .replace(/(\d+)\s*个\s*OOC/g, '$1 个角色行为突兀点')
  }

  let text = raw
    .replace(/reward/gi, '爽点')
    .replace(/minor_violence/g, '轻度暴力表达')
    .replace(/vulgar_language/g, '粗口/低俗表达')
    .replace(/high_risk/g, '高风险')
    .replace(/medium_risk/g, '中风险')
    .replace(/low_risk/g, '低风险')
    .replace(/scene_no\s*=\s*(\d{1,3})-(\d{1,3})/g, '第 $1 集第 $2 场')

  // 把 "(1-1、2-1)" / " 14-1 " 这类裸场号转成人话坐标。
  text = text.replace(/(^|[（(,，、\s])(\d{1,3})-(\d{1,3})(?=$|[）),，、\s。；;])/g, (_m, prefix, ep, sc) => {
    return `${prefix}第 ${ep} 集第 ${sc} 场`
  })

  return text
}

function groupHighlights(items: HighlightDTO[]): HighlightGroup[] {
  const buckets = new Map<HighlightType, HighlightDTO[]>()
  for (const it of items) {
    const arr = buckets.get(it.type) || []
    arr.push(it)
    buckets.set(it.type, arr)
  }
  const out: HighlightGroup[] = []
  for (const t of HIGHLIGHT_TYPE_ORDER) {
    const arr = buckets.get(t)
    if (arr && arr.length) out.push({ type: t, items: arr })
  }
  // 兜底：未在 ORDER 里的 type 也带上（向后兼容新增 type）
  for (const [t, arr] of buckets) {
    if (!HIGHLIGHT_TYPE_ORDER.includes(t)) out.push({ type: t, items: arr })
  }
  return out
}
