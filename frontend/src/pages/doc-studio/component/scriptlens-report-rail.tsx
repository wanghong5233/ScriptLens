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
 * 数据源：fetchScriptView(scriptId)，view 包含派生的 rewrite_seeds + task_status。
 *
 * Segment 顺序：速览 → 故事 → 人物 → 评估 → 行动；视角切换由「行动」segment 的
 * 三张 Persona Action Card（选品 / 编剧 / 审核）实装，详见 docs/09-action-lens.md。
 */

import {
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
import { GraphChart, LineChart } from 'echarts/charts'
import {
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  ToolboxComponent,
  GridComponent,
  MarkPointComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import ReactECharts from 'echarts-for-react/lib/core'

echarts.use([
  GraphChart,
  LineChart,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  ToolboxComponent,
  GridComponent,
  MarkPointComponent,
  CanvasRenderer,
])
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
  type ScriptViewResponseDTO,
} from '@/api/docStudio'
import { formatSceneLocator, type AgentTask, type DimensionKey } from '../agentTask'
import {
  DIMENSION_RUBRICS,
  getDimensionMeta,
  getRubricLevel,
  type RubricLevel,
} from '../evaluationRubric'
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

type ReportMode = 'overview' | 'story' | 'characters' | 'evaluation' | 'action'

const REPORT_MODE_OPTIONS: Array<{ label: string; value: ReportMode }> = [
  { label: '速览', value: 'overview' },
  { label: '故事', value: 'story' },
  { label: '人物', value: 'characters' },
  { label: '评估', value: 'evaluation' },
  { label: '行动', value: 'action' },
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

function cacheKey(scriptId: string): string {
  return scriptId
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
  const [state, setState] = useState<LoadState>(() => {
    const cached = viewCache.get(cacheKey(scriptId))
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
        const view = await fetchScriptView(scriptId, { errorToast: false })
        setState({ phase: 'ready', view, scriptTitle: detail.title })
        viewCache.set(cacheKey(scriptId), { view, scriptTitle: detail.title, cachedAt: Date.now() })
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
  }, [scriptId, onScriptDetailLoaded])

  useEffect(() => {
    let cancelled = false
    const cached = viewCache.get(cacheKey(scriptId))
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
  }, [scriptId, loadOnce, stopPolling])

  // 重新诊断 == 上传后那条全链路重新跑一遍：清缓存 + 切到 no_report 阶段，
  // 让 ScriptlensReportProgress 接管，把 6 阶段实时进度面板替换掉旧报告。
  const handleReanalyze = useCallback(async () => {
    if (!scriptId || reanalyzing) return
    stopPolling()
    setReanalyzing(true)
    try {
      await reanalyzeScript(scriptId)
      viewCache.delete(cacheKey(scriptId))
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
  onTraceEvidence,
  onClearTrace,
  onDispatchTask,
}: ReadyRailProps) {
  const [reportMode, setReportMode] = useState<ReportMode>('overview')

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
  const keySceneRefs = view.must_read_scene_ids
    .map((rid) => evidenceMap.get(rid))
    .filter((ref): ref is EvidenceRefDTO => Boolean(ref))

  // 速览传入完整 highlights，由 HighlightsSection 自身的 defaultLimit + 展开机制控制可见量
  const overviewHighlights = highlights
  const riskCount = view.risk_flags?.length ?? 0
  const beatCount = view.beat_sheet?.acts?.reduce((sum, act) => sum + (act.beats?.length || 0), 0) ?? 0

  return (
    <div className={styles.rail}>
      {/* 标题行：决策 / 综合分 / 一句话理由已下沉到「速览」segment 的 30 秒判断卡，
          顶部不再有重复 hero。详见 docs/09-action-lens.md §1。 */}
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
          {/* Hero 决策卡 + KPI + 3 优 / 3 劣 分栏 — 业内对照：抖音文心 / Linear / Bloomberg pitch deck */}
          {view.coverage_card ? (
            <CoverageCardSection
              coverage={view.coverage_card}
              decisionReason={view.decision.one_sentence_reason || null}
              overallScore={view.overall_score ?? null}
              kpis={{
                characters: view.character_graph?.nodes?.length ?? 0,
                keyScenes: keySceneRefs.length,
                beats: beatCount,
                risks: riskCount,
              }}
              evidenceBySceneId={evidenceBySceneId}
              activeEvidenceId={activeEvidenceId}
              onTraceEvidence={onTraceEvidence}
            />
          ) : null}

          {summaryText ? (
            <CollapsibleSummary text={humanizeReportText(summaryText)} />
          ) : null}

          {keySceneRefs.length > 0 ? (
            <KeyScenesSection
              evidences={keySceneRefs}
              activeEvidenceId={activeEvidenceId}
              onTraceEvidence={onTraceEvidence}
              compact
            />
          ) : null}

          {overviewHighlights.length > 0 ? (
            <HighlightsSection
              highlights={overviewHighlights}
              activeEvidenceId={activeEvidenceId}
              onTraceEvidence={onTraceEvidence}
              grouped={false}
              defaultLimit={3}
              hint="剧情抓手"
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
          {/* 改写候选段已迁移到「行动 · 编剧」卡。详见 docs/09-action-lens.md §4.2 + docs/10-rewrite-agent.md。
              评估 segment 只承载诊断信息（五力卡 / 合规卡 / 风险卡），改写动作归位行动。 */}

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

      {reportMode === 'action' ? (
        <ActionSegment
          view={view}
          evidenceMap={evidenceMap}
          activeEvidenceId={activeEvidenceId}
          onTraceEvidence={onTraceEvidence}
          onDispatchTask={onDispatchTask}
          onSwitchMode={setReportMode}
        />
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
// 行动 segment：3 张 Persona Action Card（详见 docs/09-action-lens.md §4）
// 数据 100% derive 自 ViewResponse；视角切换由这里实装，不重排报告。
// ============================================================

// 行动 segment 内置局部视角切换（详见 docs/09-action-lens.md §1）：
// 默认 'writer'——剧本创作者是 ScriptLens 最高频深度用户，选品 / 审核是判断短决策。
type PersonaKey = 'selection' | 'writer' | 'review'

const PERSONA_OPTIONS: Array<{ label: string; value: PersonaKey; hint: string }> = [
  { label: '编剧', value: 'writer', hint: '改哪段：派改写 / 追问最低分维度' },
  { label: '选品', value: 'selection', hint: '签不签：题材 / 综合分 / 合规风险' },
  { label: '审核', value: 'review', hint: '过不过：合规等级 / 红线证据' },
]

// 合规双轴：等级（badge，描述风险）+ 动作（verdict，描述处置）。业内对照：
// 内容安全审核后台（抖音 / 快手内容安全 / B 站审核）均采"风险等级 tag + 处置建议"二轴。
const COMPLIANCE_LEVEL_LABEL: Record<string, string> = {
  clean: '安全',
  low_risk: '低风险',
  medium_risk: '中风险',
  high_risk: '高风险',
}

const COMPLIANCE_VERDICT: Record<string, string> = {
  clean: '过审',
  low_risk: '修改后过',
  medium_risk: '修改后过 · 需复审',
  high_risk: '退回 · 不建议立项',
}

const COMPLIANCE_VERDICT_COLOR: Record<string, string> = {
  clean: 'green',
  low_risk: 'gold',
  medium_risk: 'orange',
  high_risk: 'red',
}

interface ActionCardCommonProps {
  view: ScriptViewResponseDTO
  evidenceMap: Map<string, EvidenceRefDTO>
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
  onDispatchTask: (task: AgentTask) => void
}

interface ActionSegmentProps extends ActionCardCommonProps {
  onSwitchMode: (mode: ReportMode) => void
}

function ActionSegment(props: ActionSegmentProps) {
  const [persona, setPersona] = useState<PersonaKey>('writer')
  const personaMeta = PERSONA_OPTIONS.find((p) => p.value === persona)

  return (
    <div className={styles.actionSegment}>
      <SectionHeader
        title="行动 · 视角切换"
        hint="默认编剧视角；切换可见选品 / 审核视角；每个视角的结论 / 证据 / Next Action 完全不同"
      />
      <div className={styles.actionPersonaSwitch}>
        <Segmented
          size="small"
          value={persona}
          options={PERSONA_OPTIONS.map((p) => ({ label: p.label, value: p.value }))}
          onChange={(next) => setPersona(next as PersonaKey)}
          block
        />
        {personaMeta ? <div className={styles.actionPersonaHint}>{personaMeta.hint}</div> : null}
      </div>
      <div style={{ marginTop: 12 }}>
        {persona === 'writer' ? <WriterActionCard {...props} /> : null}
        {persona === 'selection' ? <SelectionActionCard {...props} /> : null}
        {persona === 'review' ? <ReviewActionCard {...props} /> : null}
      </div>
    </div>
  )
}

interface ActionEvidenceItem {
  dimension: string
  score: number | null
  ref: EvidenceRefDTO
  caption: string
}

interface ActionItem {
  label: string
  type?: 'primary' | 'default'
  onClick: () => void
  disabled?: boolean
}

function ActionCardShell({
  title,
  badge,
  badgeColor,
  verdict,
  reason,
  evidence,
  hint,
  actions,
  activeEvidenceId,
  onTraceEvidence,
}: {
  title: string
  badge: string
  badgeColor: string
  // verdict 仅在有数据派生依据时传入（如编剧卡的 rewrite 段数计数 / 审核卡的 compliance.level 状态映射）。
  // 选品卡不传——签不签的理由完全来自 LLM 输出的 reason，不允许前端拼模板话术。
  verdict?: string
  reason?: string | null
  evidence: ActionEvidenceItem[]
  hint?: React.ReactNode
  actions: ActionItem[]
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
}) {
  return (
    <div className={styles.actionCard}>
      <div className={styles.actionCardHeader}>
        <Text strong className={styles.actionCardTitle}>{title}</Text>
        <Tag color={badgeColor} className={styles.actionCardBadge}>{badge}</Tag>
      </div>
      {verdict ? <div className={styles.actionCardVerdict}>{verdict}</div> : null}
      {reason ? (
        <Paragraph className={styles.actionCardReason}>{humanizeReportText(reason)}</Paragraph>
      ) : null}
      {evidence.length > 0 ? (
        <div className={styles.actionCardEvidence}>
          <Text type="secondary" className={styles.actionCardEvidenceLabel}>
            优先证据 {evidence.length}
          </Text>
          <Space direction="vertical" size={6} style={{ width: '100%', marginTop: 4 }}>
            {evidence.map((evi, i) => {
              const active = activeEvidenceId === evi.ref.id
              const locator = formatSceneLocator(
                evi.ref.episode_no ?? null,
                evi.ref.scene_no ?? null,
                evi.ref.scene_label ?? null,
              ) || evi.ref.scene_id.slice(0, 6)
              return (
                <button
                  key={`${evi.dimension}:${evi.ref.id}:${i}`}
                  type="button"
                  className={`${styles.actionCardEvidenceItem} ${active ? styles.actionCardEvidenceItemActive : ''}`}
                  onClick={() =>
                    onTraceEvidence({
                      evidenceRefId: evi.ref.id,
                      sceneId: evi.ref.scene_id,
                      startLine: evi.ref.start_line ?? null,
                      endLine: evi.ref.end_line ?? null,
                    })
                  }
                >
                  <span className={styles.actionCardEvidenceMeta}>
                    <Tag color={evi.score != null && evi.score < 5 ? 'red' : 'blue'} bordered={false}>
                      {DIMENSION_LABELS[evi.dimension] || evi.dimension}
                      {evi.score != null ? ` ${evi.score}/10` : ''}
                    </Tag>
                    <Text type="secondary" className={styles.actionCardEvidenceLocator}>{locator}</Text>
                  </span>
                  <span className={styles.actionCardEvidenceCaption}>
                    {humanizeReportText(evi.caption)}
                  </span>
                </button>
              )
            })}
          </Space>
        </div>
      ) : null}
      {hint ? <div className={styles.actionCardHint}>{hint}</div> : null}
      {actions.length > 0 ? (
        <Space wrap size={6} className={styles.actionCardActions}>
          {actions.map((a, i) => (
            <Button
              key={`${a.label}:${i}`}
              size="small"
              type={a.type ?? 'default'}
              disabled={a.disabled}
              onClick={a.onClick}
              icon={a.type === 'primary' ? <ThunderboltOutlined /> : <SearchOutlined />}
            >
              {a.label}
            </Button>
          ))}
        </Space>
      ) : null}
    </div>
  )
}

function SelectionActionCard({
  view,
  evidenceMap,
  activeEvidenceId,
  onTraceEvidence,
  onDispatchTask,
}: ActionSegmentProps) {
  const decisionInfo = DECISION_LABEL[view.decision.label] || { text: view.decision.label, color: 'default' }

  // 优先证据：按 concept → emotion → story 取每维 evidence_ref_ids[0]
  const evidence = useMemo<ActionEvidenceItem[]>(() => {
    const orders: DimensionKey[] = ['concept', 'emotion', 'story']
    const out: ActionEvidenceItem[] = []
    for (const dim of orders) {
      const sc = view.scorecard.find((s) => s.dimension === dim)
      if (!sc?.evidence_ref_ids?.length) continue
      const ref = evidenceMap.get(sc.evidence_ref_ids[0])
      if (!ref) continue
      out.push({ dimension: dim, score: sc.score, ref, caption: sc.reason })
    }
    return out.slice(0, 3)
  }, [view.scorecard, evidenceMap])

  // 关键提示：题材徽章 + 综合分 + compliance（非 clean 时）
  const hint = (
    <Space size={4} wrap>
      {(view.coverage_card?.genre || []).slice(0, 3).map((g) => (
        <Tag key={g}>{g}</Tag>
      ))}
      {view.overall_score != null ? (
        <Tag color="blue">综合 {view.overall_score.toFixed(1)}/10</Tag>
      ) : null}
      {view.compliance?.level && view.compliance.level !== 'clean' ? (
        <Tag color={COMPLIANCE_VERDICT_COLOR[view.compliance.level]}>
          合规 · {COMPLIANCE_VERDICT[view.compliance.level]}
        </Tag>
      ) : null}
    </Space>
  )

  // 让 Agent 解释整体判断：用 dim_inquiry 把 lowest 维度作为追问目标
  const lowest = useMemo(() => {
    return [...view.scorecard]
      .filter((s) => s.score != null)
      .sort((a, b) => (a.score ?? 99) - (b.score ?? 99))[0]
  }, [view.scorecard])

  return (
    <ActionCardShell
      title="选品 · 签不签"
      badge={decisionInfo.text}
      badgeColor={decisionInfo.color}
      reason={view.decision.one_sentence_reason}
      evidence={evidence}
      hint={hint}
      actions={[
        {
          label: lowest ? `追问 · ${DIMENSION_LABELS[lowest.dimension] || lowest.dimension}` : '追问 · 综合判断',
          onClick: () =>
            onDispatchTask({
              kind: 'dim_inquiry',
              dimension: (lowest?.dimension ?? 'concept') as DimensionKey,
              current_score: lowest?.score ?? null,
            }),
        },
      ]}
      activeEvidenceId={activeEvidenceId}
      onTraceEvidence={onTraceEvidence}
    />
  )
}

/**
 * 编剧卡：双层结构（hero 全剧入口 + 段级精修列表）。
 *
 * 设计动因（详见 docs/10-rewrite-agent.md §1-2）：
 * - 短剧痛点为结构性（钩子密度 / 反转齐 / 节奏方差），段级精修治标，全剧 plan 治本
 * - 业内对照：Cursor Composer / Copilot Workspace / 抖音文心剧本助手 全采 Plan-then-Execute
 * - hero 入口 primary 治本，段级列表保留治标路径，两块同屏不切 tab
 *
 * Step 1（本版）：hero 按钮 stub，提示 Step 2 上线全剧 plan-execute；
 * 段级 rewrite_seed dispatch 时填完整 brief（题材 / 综合分 / 决策 / 维度评分 / 原文 quote）。
 */
function WriterActionCard({
  view,
  evidenceMap,
  activeEvidenceId,
  onTraceEvidence,
  onDispatchTask,
  onSwitchMode,
}: ActionSegmentProps) {
  const seeds = view.rewrite_seeds || []
  const seedCount = seeds.length

  const badge = seedCount >= 5 ? `${seedCount} 段需重写` : seedCount >= 1 ? `${seedCount} 段建议优化` : '整体可保留'
  const badgeColor = seedCount >= 5 ? 'red' : seedCount >= 1 ? 'orange' : 'green'

  // 五力短板 = 最低 2 维（< 6 才算短板，否则不显示）
  const weakDims = useMemo(() => {
    return [...view.scorecard]
      .filter((s) => s.score != null && s.score < 6)
      .sort((a, b) => (a.score ?? 99) - (b.score ?? 99))
      .slice(0, 3)
  }, [view.scorecard])

  const lowest = weakDims[0]

  // 全剧基调（拼 rewrite_seed brief 用）
  const decisionLabel = DECISION_LABEL[view.decision.label]?.text ?? view.decision.label
  const genre = view.coverage_card?.genre ?? null
  const overallScore = view.overall_score ?? null

  // 派发段级改写：填完整 brief（业内共识，详见 docs/10 §3）
  const dispatchSeed = useCallback(
    (seed: RewriteSeedDTO) => {
      const ref = evidenceMap.get(seed.evidence_ref_id)
      const sc = view.scorecard.find((s) => s.dimension === seed.dimension)
      onDispatchTask({
        kind: 'rewrite_seed',
        dimension: seed.dimension,
        scene_id: seed.scene_id,
        scene_label: seed.scene_label ?? null,
        issue: humanizeReportText(seed.issue),
        evidence_ref_id: seed.evidence_ref_id,
        score: sc?.score ?? null,
        dim_reason: sc?.reason ? humanizeReportText(_firstSentence(sc.reason, 200)) : null,
        quote: ref?.quote ?? null,
        episode_no: ref?.episode_no ?? null,
        scene_no: ref?.scene_no ?? null,
        genre,
        overall_score: overallScore,
        decision_label: decisionLabel,
      })
    },
    [evidenceMap, view.scorecard, onDispatchTask, genre, overallScore, decisionLabel],
  )

  // 全剧改写计划：Step 2 路线，详见 docs/10 §5；本版按钮先 stub 提示
  const handleFulltextPlan = useCallback(() => {
    message.info({
      content: '全剧改写计划 · Plan-then-Execute 版本即将上线（详见 docs/10-rewrite-agent.md §5）',
      duration: 4,
    })
  }, [])

  return (
    <div className={styles.actionCard}>
      <div className={styles.actionCardHeader}>
        <Text strong className={styles.actionCardTitle}>编剧 · 改哪段</Text>
        <Tag color={badgeColor} className={styles.actionCardBadge}>{badge}</Tag>
      </div>

      {/* Hero · 全剧改写计划入口（治本） */}
      <div className={styles.writerHero}>
        <div className={styles.writerHeroHeader}>
          <Text strong className={styles.writerHeroTitle}>全剧改写计划</Text>
          {weakDims.length > 0 ? (
            <Text type="secondary" className={styles.writerHeroDims}>
              五力短板 ·{' '}
              {weakDims.map((d, i) => (
                <span key={d.dimension}>
                  {i > 0 ? ' / ' : ''}
                  {DIMENSION_LABELS[d.dimension] || d.dimension} {d.score}
                </span>
              ))}
            </Text>
          ) : null}
        </div>
        <Button
          type="primary"
          icon={<ThunderboltOutlined />}
          onClick={handleFulltextPlan}
          className={styles.writerHeroBtn}
          block
        >
          让 Agent 出改写计划
        </Button>
        <Text type="secondary" className={styles.writerHeroHint}>
          Plan-then-Execute · 基于五力评分逐步改造（业内对照：Cursor Composer / 抖音文心剧本助手）
        </Text>
      </div>

      {/* 段级精修列表（治标，不限 3 条） */}
      {seedCount > 0 ? (
        <div className={styles.writerSeedList}>
          <Text type="secondary" className={styles.writerSeedListLabel}>
            或者，按段精修（共 {seedCount} 段）
          </Text>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            {seeds.map((seed) => (
              <WriterSeedRow
                key={`${seed.scene_id}:${seed.dimension}`}
                seed={seed}
                evidenceMap={evidenceMap}
                taskStatus={view.task_status}
                activeEvidenceId={activeEvidenceId}
                onTraceEvidence={onTraceEvidence}
                onDispatchSeed={dispatchSeed}
              />
            ))}
          </Space>
        </div>
      ) : (
        <div className={styles.writerSeedListEmpty}>
          <Text type="secondary">无段级改写候选 · 整体已较稳，可直接出全剧风格优化计划</Text>
        </div>
      )}

      {/* 底部次级动作 */}
      <Space wrap size={6} className={styles.actionCardActions}>
        {lowest ? (
          <Button
            size="small"
            icon={<SearchOutlined />}
            onClick={() =>
              onDispatchTask({
                kind: 'dim_inquiry',
                dimension: lowest.dimension as DimensionKey,
                current_score: lowest.score ?? null,
              })
            }
          >
            追问 · {DIMENSION_LABELS[lowest.dimension] || lowest.dimension} 怎么改
          </Button>
        ) : null}
        <Button size="small" onClick={() => onSwitchMode('story')}>
          看完整节奏曲线
        </Button>
      </Space>
    </div>
  )
}

/**
 * 编剧卡内的段级精修行：dim tag + score + 集场坐标 + issue + 行内按钮。
 * 把原 RewriteSeedCard 的样式收紧，改用紧凑两栏布局适应卡内嵌套。
 */
function WriterSeedRow({
  seed,
  evidenceMap,
  taskStatus,
  activeEvidenceId,
  onTraceEvidence,
  onDispatchSeed,
}: {
  seed: RewriteSeedDTO
  evidenceMap: Map<string, EvidenceRefDTO>
  taskStatus: Record<string, RewriteTaskStatusDTO>
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
  onDispatchSeed: (seed: RewriteSeedDTO) => void
}) {
  const dimLabel = DIMENSION_LABELS[seed.dimension] || seed.dimension
  const evi = evidenceMap.get(seed.evidence_ref_id)
  const status = taskStatus[`${seed.scene_id}:${seed.dimension}`]
  const badge = renderTaskBadge(status)
  const locator = evi
    ? formatSceneLocator(evi.episode_no, evi.scene_no, evi.scene_label)
    : seed.scene_label || seed.scene_id.slice(0, 8)

  return (
    <div className={styles.writerSeedRow}>
      <div className={styles.writerSeedRowHeader}>
        <Space size={6} wrap>
          <Tag color="gold" className={styles.seedDimTag}>{dimLabel}</Tag>
          <Text className={styles.seedScene}>{locator}</Text>
        </Space>
        {badge}
      </div>
      <div className={styles.seedIssue}>{humanizeReportText(seed.issue || '（暂无 issue）')}</div>
      <Space size={6} wrap>
        <Button
          size="small"
          icon={<ThunderboltOutlined />}
          type="primary"
          ghost
          onClick={() => onDispatchSeed(seed)}
        >
          Agent 改这段
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
      </Space>
    </div>
  )
}

function ReviewActionCard({
  view,
  evidenceMap,
  activeEvidenceId,
  onTraceEvidence,
  onDispatchTask,
  onSwitchMode,
}: ActionSegmentProps) {
  const compliance = view.compliance
  const level = compliance?.level ?? null
  const verdict = level ? COMPLIANCE_VERDICT[level] : '合规审核未运行'
  const badge = level ? COMPLIANCE_LEVEL_LABEL[level] : '未评估'
  const badgeColor = level ? COMPLIANCE_VERDICT_COLOR[level] : 'default'

  // 优先证据：compliance.evidence_ref_ids[:3]
  const evidence = useMemo<ActionEvidenceItem[]>(() => {
    const out: ActionEvidenceItem[] = []
    for (const rid of (compliance?.evidence_ref_ids || []).slice(0, 3)) {
      const ref = evidenceMap.get(rid)
      if (!ref) continue
      out.push({
        dimension: 'compliance',
        score: compliance?.score ?? null,
        ref,
        caption: ref.quote || ref.scene_label || '',
      })
    }
    return out
  }, [compliance, evidenceMap])

  const hint = (
    <Space size={4} wrap>
      {compliance?.score != null ? <Tag color="blue">合规分 {compliance.score}/10</Tag> : null}
      <Tag>{`红线证据 ${compliance?.evidence_ref_ids?.length ?? 0}`}</Tag>
      {(view.risk_flags || []).slice(0, 3).map((f) => (
        <Tag key={f} color="red">{humanizeReportText(f)}</Tag>
      ))}
    </Space>
  )

  return (
    <ActionCardShell
      title="审核 · 过不过"
      badge={badge}
      badgeColor={badgeColor}
      verdict={verdict}
      reason={compliance?.reason || null}
      evidence={evidence}
      hint={hint}
      actions={[
        {
          label: '追问 · 合规审核细则',
          onClick: () =>
            onDispatchTask({
              kind: 'dim_inquiry',
              dimension: 'compliance',
              current_score: compliance?.score ?? null,
            }),
        },
        {
          label: '看完整风险列表',
          onClick: () => onSwitchMode('evaluation'),
        },
      ]}
      activeEvidenceId={activeEvidenceId}
      onTraceEvidence={onTraceEvidence}
    />
  )
}

function _firstSentence(text: string, maxLen = 80): string {
  if (!text) return ''
  let chunk = text.trim()
  for (const sep of ['\n', '。', '；', '！', '?']) {
    if (chunk.includes(sep)) {
      chunk = chunk.split(sep)[0]
      break
    }
  }
  chunk = chunk.trim()
  if (chunk.length > maxLen) chunk = chunk.slice(0, maxLen - 1) + '…'
  return chunk
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

/**
 * 速览 Hero（30 秒判断）。
 *
 * 业内对照：抖音文心剧本助手 / Linear issue overview / Bloomberg equity research /
 * Notion property header。共性 = Hero block（决策大字）→ KPI row（数字加粗）→
 * 3 优 / 3 劣（左右分栏）。
 *
 * 当前实装层级（自顶向下）：
 *   ① Hero 行：decision badge + 综合分大字 + 题材 chips
 *   ② Logline：1 句标题
 *   ③ Reason callout：decision.one_sentence_reason（带左侧色条）
 *   ④ KPI row：4 个数字（人物 / 关键场 / 节拍 / 风险）
 *   ⑤ Core value：核心价值一行（小字 + label）
 *   ⑥ Strengths / Concerns：左右分栏，每条 ≤ 1 行 + tone icon
 */
function CoverageCardSection({
  coverage,
  decisionReason,
  overallScore,
  kpis,
  evidenceBySceneId,
  activeEvidenceId,
  onTraceEvidence,
}: {
  coverage: NonNullable<ScriptViewResponseDTO['coverage_card']>
  decisionReason: string | null
  overallScore: number | null
  kpis: { characters: number; keyScenes: number; beats: number; risks: number }
  evidenceBySceneId: Map<string, EvidenceRefDTO>
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
}) {
  const info = DECISION_LABEL[coverage.recommendation] || { text: coverage.recommendation, color: 'default' }
  const overallColor =
    overallScore == null ? '#bfbfbf' : overallScore >= 7 ? '#52c41a' : overallScore >= 5 ? '#faad14' : '#ff4d4f'

  const renderPoint = (
    point: { title: string; detail: string; anchor_scene_id?: string | null },
    tone: 'good' | 'risk',
  ) => {
    const evi = point.anchor_scene_id ? evidenceBySceneId.get(point.anchor_scene_id) : undefined
    const active = !!(evi && activeEvidenceId === evi.id)
    return (
      <button
        key={`${tone}:${point.title}:${point.anchor_scene_id || ''}`}
        type="button"
        className={`${styles.spcItem} ${tone === 'good' ? styles.spcGood : styles.spcRisk} ${
          active ? styles.spcItemActive : ''
        }`}
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
        <span className={styles.spcTitle}>{point.title}</span>
        <span className={styles.spcDetail}>{humanizeReportText(point.detail)}</span>
      </button>
    )
  }

  const strengths = (coverage.strengths || []).slice(0, 3)
  const concerns = (coverage.concerns || []).slice(0, 3)

  return (
    <section className={styles.heroSection}>
      <SectionHeader title="30 秒判断" hint="先判断值不值得继续读" />

      {/* ① Hero 行 */}
      <div className={styles.heroRow}>
        <div className={styles.heroLeft}>
          <Tag color={info.color} className={styles.decisionTag}>{info.text}</Tag>
          {coverage.genre?.slice(0, 3).map((g) => (
            <Tag key={g} className={styles.heroGenreTag}>{g}</Tag>
          ))}
        </div>
        {overallScore != null ? (
          <div className={styles.heroScore}>
            <span className={styles.heroScoreNum} style={{ color: overallColor }}>
              {overallScore.toFixed(1)}
            </span>
            <span className={styles.heroScoreUnit}>/10</span>
          </div>
        ) : null}
      </div>

      {/* ② Logline */}
      <Paragraph className={styles.heroLogline}>{humanizeReportText(coverage.logline)}</Paragraph>

      {/* ③ Reason callout */}
      {decisionReason ? (
        <div className={styles.heroReasonCallout}>
          <span className={styles.heroReasonText}>{humanizeReportText(decisionReason)}</span>
        </div>
      ) : null}

      {/* ④ KPI row */}
      <div className={styles.heroKpiRow}>
        <KpiCell num={kpis.characters} label="关键人物" />
        <KpiCell num={kpis.keyScenes} label="关键场景" />
        <KpiCell num={kpis.beats} label="故事节拍" />
        <KpiCell num={kpis.risks} label="风险标签" tone={kpis.risks > 0 ? 'risk' : 'neutral'} />
      </div>

      {/* ⑤ Core value */}
      {coverage.core_value ? (
        <div className={styles.heroCoreValue}>
          <span className={styles.heroCoreValueLabel}>核心价值</span>
          <span className={styles.heroCoreValueText}>{humanizeReportText(coverage.core_value)}</span>
        </div>
      ) : null}

      {/* ⑥ Strengths / Concerns 左右分栏 */}
      {(strengths.length > 0 || concerns.length > 0) ? (
        <div className={styles.spcGrid}>
          <div className={styles.spcCol}>
            <div className={styles.spcColHeader}>
              <span className={styles.spcDot} style={{ background: '#52c41a' }} />
              <Text type="secondary" className={styles.spcColTitle}>亮点 {strengths.length}</Text>
            </div>
            {strengths.map((p) => renderPoint(p, 'good'))}
          </div>
          <div className={styles.spcCol}>
            <div className={styles.spcColHeader}>
              <span className={styles.spcDot} style={{ background: '#fa8c16' }} />
              <Text type="secondary" className={styles.spcColTitle}>风险 {concerns.length}</Text>
            </div>
            {concerns.map((p) => renderPoint(p, 'risk'))}
          </div>
        </div>
      ) : null}
    </section>
  )
}

function KpiCell({
  num,
  label,
  tone = 'neutral',
}: {
  num: number
  label: string
  tone?: 'neutral' | 'risk'
}) {
  return (
    <div className={styles.kpiCell}>
      <span
        className={styles.kpiNum}
        style={{ color: tone === 'risk' && num > 0 ? '#cf1322' : '#3F3835' }}
      >
        {num}
      </span>
      <span className={styles.kpiLabel}>{label}</span>
    </div>
  )
}

/**
 * 剧本概览：默认折叠（业内对照：Linear / GitHub PR summary / Notion toggle block）。
 * 长散文不应在 30 秒判断面板首屏强塞——读者已经从 Hero / KPI / 优劣分栏获取了
 * 决策所需的全部信号；3-5 句概要属于"想看才看"的二级信息。
 */
function CollapsibleSummary({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <section className={styles.summarySection}>
      <button
        type="button"
        className={styles.collapseToggle}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <Text strong className={styles.sectionTitle}>剧本概览</Text>
        <Text type="secondary" className={styles.collapseHint}>
          {open ? '收起' : '展开 · 3-5 句概要'}
        </Text>
      </button>
      {open ? (
        <Paragraph className={styles.summaryText} style={{ marginTop: 8 }}>
          {text}
        </Paragraph>
      ) : null}
    </section>
  )
}

/**
 * 故事骨架（三幕横向卡片 + 节拍 chip 点击展开 / 跳原文）。
 *
 * 业内对照：Final Draft Story Map / Save the Cat Beat Sheet /
 * Sudowrite Manuscript Analysis / 抖音文心节拍图谱。共性 = 横向时间轴 > 纵向列表，
 * 每幕一栏（开局 / 发展 / 收束），节拍 chip 点击展开详情或跳原文。
 *
 * 旧设计是纵向"幕标签 + 节拍按钮"列表（每节拍占一整行 = 7 行），扫一眼无法看清三幕骨架；
 * 新设计三栏卡片 + chip 行让"看到三幕完整 / 看到每幕几个节拍"成为视觉一目了然。
 */
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
  // 节拍类型 → 中文标签 + 颜色（与 docs/05 §6 / docs/08 §3.1 五个关键节拍同步）
  const BEAT_LABEL: Record<string, string> = {
    opening: '开场',
    inciting: '激励',
    midpoint: '中点',
    twist: '反转',
    reward: '爽点',
    climax: '高潮',
    closing: '收束',
  }
  return (
    <section className={styles.beatSection}>
      <SectionHeader title="故事骨架" hint="三幕结构 + 关键节拍；点击节拍跳原文" />
      <div className={styles.beatActsRow}>
        {acts.map((act) => (
          <div key={act.act} className={styles.beatActCol}>
            <div className={styles.beatActHeader}>
              <Tag color="purple" className={styles.beatActTag}>
                第 {act.act} 幕
              </Tag>
              <span className={styles.beatActTitle}>{act.title}</span>
              <span className={styles.beatActCount}>{act.beats.length} 节拍</span>
            </div>
            <div className={styles.beatChipList}>
              {act.beats.map((beat) => {
                const evi = evidenceBySceneId.get(beat.anchor_scene_id)
                const active = !!(evi && activeEvidenceId === evi.id)
                const beatLabel = BEAT_LABEL[beat.type] || beat.type
                return (
                  <BeatChip
                    key={`${act.act}:${beat.type}:${beat.anchor_scene_id}`}
                    typeLabel={beatLabel}
                    summary={humanizeReportText(beat.summary)}
                    active={active}
                    onTrace={() => {
                      if (!evi) return
                      onTraceEvidence({
                        evidenceRefId: evi.id,
                        sceneId: evi.scene_id,
                        startLine: evi.start_line ?? null,
                        endLine: evi.end_line ?? null,
                      })
                    }}
                  />
                )
              })}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

/**
 * 节拍 chip：默认收起（type chip + ≤ 30 字一句话），点击展开详情或跳原文。
 * 业内对照：Final Draft / Sudowrite 节拍点 click 行为。
 */
function BeatChip({
  typeLabel,
  summary,
  active,
  onTrace,
}: {
  typeLabel: string
  summary: string
  active: boolean
  onTrace: () => void
}) {
  const [open, setOpen] = useState(false)
  // summary 短就直接渲染（不需要展开）；长就一行省略 + 点击展开
  const isLong = summary.length > 30
  return (
    <div className={`${styles.beatChip} ${active ? styles.beatChipActive : ''}`}>
      <button
        type="button"
        className={styles.beatChipHead}
        onClick={() => {
          if (isLong) setOpen((v) => !v)
          else onTrace()
        }}
      >
        <Tag color="geekblue" className={styles.beatChipTypeTag}>
          {typeLabel}
        </Tag>
        <span className={`${styles.beatChipSummary} ${open ? styles.beatChipSummaryFull : ''}`}>
          {summary}
        </span>
      </button>
      {isLong && open ? (
        <button type="button" className={styles.beatChipJumpBtn} onClick={onTrace}>
          跳原文 →
        </button>
      ) : null}
    </div>
  )
}

/**
 * 节奏曲线（ECharts line chart）。
 *
 * 业内对照：YouTube Studio analytics / Bloomberg / Tableau 时序数据可视化 /
 * 抖音文心剧本助手集数密度图谱。共性 = x 集数 / y 事件密度 / spike 标红 / hover 看具体值。
 *
 * 取代旧的"每集一行 Progress 条"——100 集 = 100 行的纵向列表无法表达整体趋势，
 * 也无法快速定位 spike / 塌陷段。折线图自动 binning（ECharts dataZoom）可直接拖。
 */
function PacingCurveSection({
  points,
}: {
  points: NonNullable<ScriptViewResponseDTO['pacing_curve']>
}) {
  const maxEvent = Math.max(1, ...points.map((p) => p.event_count || 0))
  // 平均值：让用户判断"低于 / 高于平均"
  const avgEvent = points.length > 0
    ? points.reduce((sum, p) => sum + (p.event_count || 0), 0) / points.length
    : 0

  // spike 阈值：取最大值的 70% 作为 spike 标记（与原 Progress 配色阈值一致）
  const spikeThreshold = maxEvent * 0.7

  // ECharts 数据
  const xData = points.map((p) => `第 ${p.episode_no} 集`)
  const yData = points.map((p) => p.event_count || 0)

  // markPoint 高亮 spike（事件数 ≥ 70% maxEvent）
  const spikeMarkPoints = points
    .map((p, idx) => ({ p, idx }))
    .filter(({ p }) => p.event_count >= spikeThreshold && p.event_count > 0)
    .map(({ idx }) => ({ coord: [idx, yData[idx]] }))

  const option = {
    grid: { top: 28, right: 16, bottom: 36, left: 36, containLabel: true },
    tooltip: {
      trigger: 'axis' as const,
      backgroundColor: '#FFFDFB',
      borderColor: '#E8DDD3',
      textStyle: { color: '#3F3835', fontSize: 12 },
      formatter: (params: { name: string; value: number }[]) => {
        if (!params || !params.length) return ''
        const { name, value } = params[0]
        const above = value > avgEvent ? '↑ 高于平均' : value < avgEvent ? '↓ 低于平均' : '= 平均'
        const tag = value >= spikeThreshold && value > 0 ? '<span style="color:#cf1322">· spike</span>' : ''
        return `${name}<br/>事件密度 <b>${value}</b> · ${above} ${tag}`
      },
    },
    xAxis: {
      type: 'category' as const,
      data: xData,
      axisLabel: { color: '#9C8E89', fontSize: 10, interval: Math.max(0, Math.floor(points.length / 12) - 1) },
      axisLine: { lineStyle: { color: '#ECDFCE' } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value' as const,
      name: '事件密度',
      nameTextStyle: { color: '#9C8E89', fontSize: 10 },
      axisLabel: { color: '#9C8E89', fontSize: 10 },
      axisLine: { show: false },
      splitLine: { lineStyle: { color: '#F1ECE7', type: 'dashed' as const } },
    },
    series: [
      {
        type: 'line' as const,
        data: yData,
        smooth: true,
        symbol: 'circle',
        symbolSize: 4,
        lineStyle: { color: '#E07A8C', width: 2 },
        itemStyle: { color: '#E07A8C' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(224, 122, 140, 0.25)' },
            { offset: 1, color: 'rgba(224, 122, 140, 0.02)' },
          ]),
        },
        markPoint: spikeMarkPoints.length > 0
          ? {
              symbol: 'pin',
              symbolSize: 28,
              data: spikeMarkPoints,
              itemStyle: { color: '#cf1322' },
              label: { color: '#FFF', fontSize: 10, formatter: (params: { value: number }) => `${params.value}` },
            }
          : undefined,
        markLine: avgEvent > 0
          ? {
              silent: true,
              symbol: 'none',
              lineStyle: { color: '#B0A39E', type: 'dashed' as const, width: 1 },
              label: {
                position: 'end' as const,
                formatter: `平均 ${avgEvent.toFixed(1)}`,
                color: '#9C8E89',
                fontSize: 10,
              },
              data: [{ yAxis: avgEvent }],
            }
          : undefined,
      },
    ],
  }

  return (
    <section className={styles.highlightsSection}>
      <SectionHeader
        title="节奏曲线"
        hint={`${points.length} 集 · spike ${spikeMarkPoints.length} 处 · 平均 ${avgEvent.toFixed(1)}`}
      />
      <div className={styles.pacingChartWrap}>
        <ReactECharts
          echarts={echarts}
          option={option}
          style={{ height: 220, width: '100%' }}
          notMerge
          lazyUpdate
        />
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

/**
 * 评估维度卡（可展开 · 业内对照见 docs/08 §3 + frontend evaluationRubric.ts）。
 *
 * 收起态（30 秒判断）：维度名 + 副标题 + 档位 tag + 分数大字 + reason 1 句 + 证据 chip
 * 展开态（追问论据）：上面 + 完整 reason + Rubric 4 档表（高亮当前档）+ 证据列表完整版 + 追问 Agent
 *
 * 业内对照（rubric-based 评分卡片）：
 *   - 学术 peer review (Elsevier / EditPro)：rubric 等级 + 评审意见 + 引用证据
 *   - Coursera Smart Review / Khan Academy AI 评分：rubric tier + 改进建议
 *   - Grammarly / DeepL Write："Why this rating?" 展开 + rubric + 重写
 *   - Sudowrite Manuscript Analysis：每维 rubric 锚点 + 5 条证据
 */
function DimCard({
  item,
  evidenceMap,
  activeEvidenceId,
  onTraceEvidence,
  onDispatchTask,
}: DimCardProps) {
  const [expanded, setExpanded] = useState(false)
  const label = DIMENSION_LABELS[item.dimension] || item.dimension
  const meta = getDimensionMeta(item.dimension)
  const subtitle = meta?.subtitle || DIMENSION_HINTS[item.dimension] || ''
  const isNoScore = item.score === null || item.score === undefined
  const evidences: EvidenceRefDTO[] = (item.evidence_ref_ids || [])
    .map((rid) => evidenceMap.get(rid))
    .filter((x): x is EvidenceRefDTO => x !== undefined)

  const currentLevel = getRubricLevel(item.score)
  const rubrics =
    item.dimension in DIMENSION_RUBRICS ? DIMENSION_RUBRICS[item.dimension as DimensionKey] : []

  // 收起态 reason 取首句；展开态显示全文（fail aloud：null 不渲染）
  const reasonFirst = item.reason ? humanizeReportText(_firstSentence(item.reason, 120)) : ''
  const reasonFull = item.reason ? humanizeReportText(item.reason) : ''

  return (
    <div className={`${styles.dimCard} ${expanded ? styles.dimCardExpanded : ''}`}>
      <button
        type="button"
        className={styles.dimCardHeaderBtn}
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <span className={styles.dimHeaderLeft}>
          <span className={styles.dimLabel}>{label}</span>
          {subtitle ? <span className={styles.dimHint}>· {subtitle}</span> : null}
        </span>
        <span className={styles.dimHeaderRight}>
          {item.level ? (
            <Tag color={LEVEL_COLOR[item.level] || 'default'} className={styles.dimLevelTag}>
              {LEVEL_LABEL[item.level] || item.level}
            </Tag>
          ) : null}
          <span className={styles.dimScore}>{isNoScore ? '—' : `${item.score} / 10`}</span>
          <span className={styles.dimExpandIcon}>{expanded ? '▴' : '▾'}</span>
        </span>
      </button>

      {/* 收起态 reason 一句话 + 证据 chip 简版 */}
      {!expanded && reasonFirst ? (
        <div className={styles.dimReasonOneLine}>{reasonFirst}</div>
      ) : null}
      {!expanded && evidences.length > 0 ? (
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
      ) : null}

      {/* 展开态 */}
      {expanded ? (
        <div className={styles.dimDetail}>
          {/* Rubric 锚点表 */}
          {rubrics.length > 0 ? (
            <div className={styles.dimDetailBlock}>
              <div className={styles.dimDetailLabel}>Rubric 锚点 · 当前 {TAG_RANGE[currentLevel]}</div>
              <div className={styles.rubricList}>
                {rubrics.map((r) => (
                  <div
                    key={r.level}
                    className={`${styles.rubricRow} ${
                      r.level === currentLevel ? styles.rubricRowActive : ''
                    }`}
                  >
                    <Tag color={r.color} className={styles.rubricTag}>
                      {r.range} · {r.tag}
                    </Tag>
                    <span className={styles.rubricSignals}>{r.signals.join(' / ')}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {/* 评分推理（完整 reason 不截断） */}
          {reasonFull ? (
            <div className={styles.dimDetailBlock}>
              <div className={styles.dimDetailLabel}>评分推理</div>
              <div className={styles.dimReasonFull}>{reasonFull}</div>
            </div>
          ) : null}

          {/* 维度定义 + 信号源 */}
          {meta ? (
            <div className={styles.dimDetailBlock}>
              <div className={styles.dimDetailLabel}>评分依据</div>
              <ul className={styles.dimSignalList}>
                {meta.signals.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {/* 证据列表完整版 */}
          {evidences.length > 0 ? (
            <div className={styles.dimDetailBlock}>
              <div className={styles.dimDetailLabel}>证据 {evidences.length}</div>
              <div className={styles.evidenceList}>
                {evidences.map((ref) => (
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
            </div>
          ) : null}

          {/* 追问 Agent */}
          <Button
            size="small"
            icon={<SearchOutlined />}
            block
            className={styles.dimInquiryFullBtn}
            onClick={() =>
              onDispatchTask({
                kind: 'dim_inquiry',
                dimension: item.dimension,
                current_score: item.score ?? null,
              })
            }
          >
            追问 Agent · 「{label}」凭什么这么打分？
          </Button>
        </div>
      ) : null}
    </div>
  )
}

const TAG_RANGE: Record<RubricLevel, string> = {
  high: '9-10 · 优秀',
  good: '6-8 · 良好',
  medium: '3-5 · 中等',
  low: '0-2 · 待改',
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
  compact = false,
}: {
  evidences: EvidenceRefDTO[]
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
  /** 速览模式：单行紧凑（标题 + ≤30 字 + →）；故事 / 评估 segment 用完整 mode */
  compact?: boolean
}) {
  return (
    <section className={styles.mustReadSection}>
      <SectionHeader
        title="关键场景"
        hint={`Top ${evidences.length} · 点击跳原文`}
      />
      <Space direction="vertical" size={compact ? 4 : 6} style={{ width: '100%', marginTop: 8 }}>
        {evidences.map((ref) => (
          <MustReadChip
            key={ref.id}
            evidence={ref}
            active={activeEvidenceId === ref.id}
            compact={compact}
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
  compact = false,
  onTrace,
}: {
  evidence: EvidenceRefDTO
  active: boolean
  compact?: boolean
  onTrace: () => void
}) {
  const locator = formatSceneLocator(evidence.episode_no, evidence.scene_no, evidence.scene_label)
  const summary = evidence.scene_summary || evidence.quote || ''
  if (compact) {
    // 业内对照（Linear / Notion 关联条目 / Bloomberg related stories）：
    // 速览的关键场景应是缩略（一行 = locator + ≤30 字一句话 + → 跳转），不是 mini 详情。
    return (
      <button
        type="button"
        className={`${styles.mustReadItemCompact} ${active ? styles.mustReadItemActive : ''}`}
        onClick={onTrace}
      >
        <Tag color={active ? 'magenta' : 'purple'} className={styles.mustReadSceneTag}>
          {locator || (evidence.scene_id || '').slice(0, 8)}
        </Tag>
        <span className={styles.mustReadQuoteCompact}>
          {summary ? humanizeReportText(summary) : '（无场景摘要）'}
        </span>
        <span className={styles.mustReadArrow}>→</span>
      </button>
    )
  }
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
  defaultLimit,
}: {
  highlights: HighlightDTO[]
  activeEvidenceId: string | null
  onTraceEvidence: Props['onTraceEvidence']
  grouped: boolean
  groups?: HighlightGroup[]
  hint: string
  /** 速览模式：默认只渲染 top-K，超出有"展开看全部"按钮（业内对照：Linear / GitHub PR） */
  defaultLimit?: number
}) {
  const [expanded, setExpanded] = useState(false)
  if (!highlights.length) return null

  const useLimit = !grouped && defaultLimit != null && defaultLimit > 0
  const visible = useLimit && !expanded ? highlights.slice(0, defaultLimit) : highlights
  const hiddenCount = useLimit && !expanded ? Math.max(highlights.length - defaultLimit, 0) : 0

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
          <div className={styles.highlightItems}>{visible.map(renderRow)}</div>
        )}
      </div>
      {hiddenCount > 0 ? (
        <button
          type="button"
          className={styles.highlightExpandBtn}
          onClick={() => setExpanded(true)}
        >
          看全部 {highlights.length} 个 ({hiddenCount} 个折叠中)
        </button>
      ) : null}
      {useLimit && expanded ? (
        <button
          type="button"
          className={styles.highlightExpandBtn}
          onClick={() => setExpanded(false)}
        >
          收起
        </button>
      ) : null}
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

// 旧的 RewriteSeedCard 在 v2 行动 lens 改造中被 WriterSeedRow 取代——
// 段级改写是「行动 · 编剧」职责，不再放在评估 segment。详见 docs/10-rewrite-agent.md §1。

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
