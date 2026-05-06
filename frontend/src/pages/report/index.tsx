import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  message,
  Progress,
  Result,
  Row,
  Skeleton,
  Space,
  Spin,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useSnapshot } from 'valtio'
import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  fetchScriptDetail,
  fetchScriptReport,
  fetchScriptView,
  isReportReady,
  reanalyzeScript,
  type EvidenceRefDTO,
  type ScorecardItemDTO,
  type ScriptViewResponseDTO,
  type ScriptViewRole,
} from '@/api/docStudio'
import FeedbackButton from '@/components/FeedbackButton'
import { userState } from '@/store/user'
import styles from './index.module.scss'

const { Title, Paragraph, Text } = Typography

// ====================== 维度元数据 ======================

interface DimensionMeta {
  key: string
  label: string
  shortLabel: string
  hint: string
}

const DIMENSION_META: Record<string, DimensionMeta> = {
  opening_hook: {
    key: 'opening_hook',
    label: '开场钩子',
    shortLabel: '钩子',
    hint: '前 3 集前 3 场是否抓人（rubric §3.1）',
  },
  reward_density: {
    key: 'reward_density',
    label: '爽点密度',
    shortLabel: '爽点',
    hint: '反转 / 打脸 / 逆袭密度（rubric §3.2）',
  },
  motivation: {
    key: 'motivation',
    label: '动机自洽',
    shortLabel: '动机',
    hint: '关键决策是否有铺垫（rubric §3.3）',
  },
  pacing: {
    key: 'pacing',
    label: '节奏控制',
    shortLabel: '节奏',
    hint: '中段是否塌陷（rubric §3.4）',
  },
  risk: {
    key: 'risk',
    label: '审核风险',
    shortLabel: '风险',
    hint: '广电红线 / 题材风险（rubric §3.5）',
  },
}

const LEVEL_COLOR: Record<string, string> = {
  high: 'green',
  medium: 'orange',
  low: 'red',
  clean: 'cyan',
  high_risk: 'red',
  medium_risk: 'orange',
  low_risk: 'gold',
  minor: 'orange',
  major: 'red',
}

const LEVEL_LABEL: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
  clean: '安全',
  high_risk: '高风险',
  medium_risk: '中风险',
  low_risk: '低风险',
  minor: '次要',
  major: '严重',
}

const ROLE_TABS: { key: ScriptViewRole; label: string; description: string }[] = [
  {
    key: 'selection',
    label: '选品视角',
    description: '钩子 → 爽点 → 风险 → 节奏 → 动机',
  },
  {
    key: 'writer',
    label: '编剧视角',
    description: '动机 → 节奏 → 钩子 → 爽点 → 风险',
  },
  {
    key: 'review',
    label: '审核视角',
    description: '风险 → 动机 → 节奏 → 钩子 → 爽点',
  },
]

const DECISION_LABEL_MAP: Record<string, { text: string; color: string }> = {
  recommend: { text: '推荐立项', color: 'green' },
  cautious_continue: { text: '审慎推进', color: 'orange' },
  refer_for_rewrite: { text: '建议改写', color: 'gold' },
  not_recommend: { text: '不建议立项', color: 'red' },
}

const CONFIDENCE_LABEL: Record<string, string> = {
  high: '置信度高',
  medium: '置信度中',
  low: '置信度低',
}

// ====================== 组件 ======================

type LoadState =
  | { phase: 'loading' }
  | { phase: 'auth_required' }
  | { phase: 'not_ready'; status: string; failureReason?: string | null }
  | { phase: 'no_report'; scriptStatus: string }
  | { phase: 'analyzing' }
  | { phase: 'ready'; view: ScriptViewResponseDTO; scriptTitle: string }
  | { phase: 'error'; error: string }

export default function ReportPage() {
  const { scriptId } = useParams<{ scriptId: string }>()
  const navigate = useNavigate()
  const user = useSnapshot(userState)

  const [role, setRole] = useState<ScriptViewRole>('selection')
  const [state, setState] = useState<LoadState>({ phase: 'loading' })
  const [reanalyzing, setReanalyzing] = useState(false)
  const pollTimerRef = useRef<number | null>(null)

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }, [])

  const loadView = useCallback(
    async (currentRole: ScriptViewRole) => {
      if (!scriptId) {
        setState({ phase: 'error', error: '缺少 scriptId 参数' })
        return
      }
      if (!user.token) {
        setState({ phase: 'auth_required' })
        return
      }

      try {
        const detail = await fetchScriptDetail(scriptId)
        if (detail.status !== 'ready') {
          setState({
            phase: 'not_ready',
            status: detail.status,
            failureReason: detail.failure_reason,
          })
          if (detail.status === 'pending' || detail.status === 'parsing' || detail.status === 'indexing') {
            scheduleNextPoll(currentRole)
          }
          return
        }

        // status=ready：先尝试拿 view（已生成报告时直接 200）
        try {
          const view = await fetchScriptView(scriptId, currentRole)
          stopPolling()
          setState({ phase: 'ready', view, scriptTitle: detail.title })
          return
        } catch (err: unknown) {
          // 409 表示报告未生成 / 不存在；其他状态码直接抛
          const status = (err as { response?: { status?: number } })?.response?.status
          if (status !== 409) throw err
        }

        // status=ready 但 reports 表无数据：让用户点 "立即生成评分"
        const report = await fetchScriptReport(scriptId)
        if (isReportReady(report)) {
          // 跑到这里说明 view 409 但 report 又有，理论不会发生，重试一次 view
          const view = await fetchScriptView(scriptId, currentRole)
          stopPolling()
          setState({ phase: 'ready', view, scriptTitle: detail.title })
        } else {
          stopPolling()
          setState({ phase: 'no_report', scriptStatus: detail.status })
        }
      } catch (err: unknown) {
        const e = err as { response?: { status?: number; data?: { detail?: string } }; message?: string }
        if (e?.response?.status === 401) {
          setState({ phase: 'auth_required' })
          return
        }
        setState({
          phase: 'error',
          error: e?.response?.data?.detail || e?.message || '加载失败',
        })
      }
    },
    [scriptId, user.token, stopPolling],
  )

  const scheduleNextPoll = useCallback(
    (currentRole: ScriptViewRole) => {
      stopPolling()
      pollTimerRef.current = window.setTimeout(() => {
        loadView(currentRole)
      }, 3000)
    },
    [loadView, stopPolling],
  )

  useEffect(() => {
    loadView(role)
    return () => stopPolling()
  }, [loadView, role, stopPolling])

  const handleReanalyze = useCallback(async () => {
    if (!scriptId) return
    setReanalyzing(true)
    try {
      await reanalyzeScript(scriptId)
      message.success('已触发重新评分，约 5 秒后完成')
      setState({ phase: 'analyzing' })
      // 启动轮询直到 view 拉到为止
      const tick = async () => {
        try {
          const r = await fetchScriptReport(scriptId)
          if (isReportReady(r)) {
            const view = await fetchScriptView(scriptId, role)
            const detail = await fetchScriptDetail(scriptId)
            stopPolling()
            setState({ phase: 'ready', view, scriptTitle: detail.title })
            return
          }
        } catch (e) {
          // ignore，继续轮
        }
        pollTimerRef.current = window.setTimeout(tick, 3000)
      }
      pollTimerRef.current = window.setTimeout(tick, 2000)
    } catch (err: unknown) {
      const e = err as { response?: { data?: { detail?: string } }; message?: string }
      message.error(`触发评分失败：${e?.response?.data?.detail || e?.message || '未知错误'}`)
    } finally {
      setReanalyzing(false)
    }
  }, [scriptId, role, stopPolling])

  // ====================== 渲染分支 ======================

  if (state.phase === 'loading') {
    return (
      <PageShell
        scriptId={scriptId}
        scriptTitle="加载中..."
        onBack={() => navigate('/doc-studio')}
        onReanalyze={null}
      >
        <Skeleton active paragraph={{ rows: 8 }} />
      </PageShell>
    )
  }

  if (state.phase === 'auth_required') {
    return (
      <PageShell
        scriptId={scriptId}
        scriptTitle="请先登录"
        onBack={() => navigate('/')}
        onReanalyze={null}
      >
        <Result
          status="403"
          title="需要登录"
          subTitle="查看分析报告需要先登录账号"
          extra={
            <Button
              type="primary"
              onClick={() =>
                navigate(`/login?redirect=${encodeURIComponent(`/scripts/${scriptId}/report`)}`)
              }
            >
              立即登录
            </Button>
          }
        />
      </PageShell>
    )
  }

  if (state.phase === 'error') {
    return (
      <PageShell
        scriptId={scriptId}
        scriptTitle="加载失败"
        onBack={() => navigate('/doc-studio')}
        onReanalyze={null}
      >
        <Result
          status="error"
          title="无法加载报告"
          subTitle={state.error}
          extra={
            <Button onClick={() => loadView(role)} icon={<ReloadOutlined />}>
              重试
            </Button>
          }
        />
      </PageShell>
    )
  }

  if (state.phase === 'not_ready') {
    const isFailed = state.status === 'failed'
    return (
      <PageShell
        scriptId={scriptId}
        scriptTitle="解析中"
        onBack={() => navigate('/doc-studio')}
        onReanalyze={null}
      >
        {isFailed ? (
          <Result
            status="error"
            title="剧本解析失败"
            subTitle={state.failureReason || '后台未给出失败原因'}
            extra={
              <Button onClick={() => loadView(role)} icon={<ReloadOutlined />}>
                重新加载
              </Button>
            }
          />
        ) : (
          <Card variant="borderless" className={styles.statusCard}>
            <Spin tip={`剧本 status=${state.status}，正在切集 / 切场 / 入库...`}>
              <div style={{ height: 96 }} />
            </Spin>
            <Paragraph type="secondary" className={styles.statusHint}>
              首次解析通常 4-8 秒，切场后会自动开始 5 维评分。
              页面每 3 秒自动刷新。
            </Paragraph>
          </Card>
        )}
      </PageShell>
    )
  }

  if (state.phase === 'no_report') {
    return (
      <PageShell
        scriptId={scriptId}
        scriptTitle="未生成报告"
        onBack={() => navigate('/doc-studio')}
        onReanalyze={null}
      >
        <Result
          icon={<Empty description={false} />}
          title="尚未生成 5 维评分报告"
          subTitle="剧本已切场入库（status=ready），但还没跑过评分流水线。点击下方按钮触发。"
          extra={
            <Button
              type="primary"
              icon={<ReloadOutlined />}
              loading={reanalyzing}
              onClick={handleReanalyze}
            >
              立即生成评分
            </Button>
          }
        />
      </PageShell>
    )
  }

  if (state.phase === 'analyzing') {
    return (
      <PageShell
        scriptId={scriptId}
        scriptTitle="正在评分"
        onBack={() => navigate('/doc-studio')}
        onReanalyze={null}
      >
        <Card variant="borderless" className={styles.statusCard}>
          <Spin tip="LLM 正在跑 5 维评分..." size="large">
            <div style={{ height: 120 }} />
          </Spin>
          <Paragraph type="secondary" className={styles.statusHint}>
            通常 4-6 秒；超过 30 秒未返回会自动停止轮询。
          </Paragraph>
        </Card>
      </PageShell>
    )
  }

  // phase=ready
  return (
    <PageShell
      scriptId={scriptId}
      scriptTitle={state.scriptTitle}
      onBack={() => navigate('/doc-studio')}
      onReanalyze={handleReanalyze}
      reanalyzing={reanalyzing}
    >
      <ReportContent
        scriptId={scriptId!}
        view={state.view}
        currentRole={role}
        onRoleChange={(r) => setRole(r)}
      />
    </PageShell>
  )
}

// ====================== Shell（顶部 + 内容容器）======================

interface PageShellProps {
  scriptId?: string
  scriptTitle: string
  onBack: () => void
  onReanalyze: (() => void) | null
  reanalyzing?: boolean
  children: React.ReactNode
}

function PageShell({
  scriptId,
  scriptTitle,
  onBack,
  onReanalyze,
  reanalyzing,
  children,
}: PageShellProps) {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Space size={12}>
          <Button type="text" icon={<ArrowLeftOutlined />} onClick={onBack}>
            返回 doc-studio
          </Button>
          <span className={styles.divider} />
          <span className={styles.brandMark}>SL</span>
          <Title level={4} className={styles.title}>
            {scriptTitle}
            <Text type="secondary" className={styles.subTitle}>
              · 分析报告
            </Text>
          </Title>
        </Space>
        <Space>
          {scriptId ? (
            <Tag>
              script_id: <Text code>{scriptId.slice(0, 8)}</Text>
            </Tag>
          ) : null}
          {onReanalyze ? (
            <Tooltip title="重新跑 5 维评分（覆盖旧报告）">
              <Button
                icon={<ReloadOutlined />}
                onClick={onReanalyze}
                loading={reanalyzing}
              >
                重新评分
              </Button>
            </Tooltip>
          ) : null}
        </Space>
      </header>
      <main className={styles.main}>{children}</main>
    </div>
  )
}

// ====================== 报告主体 ======================

interface ReportContentProps {
  scriptId: string
  view: ScriptViewResponseDTO
  currentRole: ScriptViewRole
  onRoleChange: (role: ScriptViewRole) => void
}

function ReportContent({ scriptId, view, currentRole, onRoleChange }: ReportContentProps) {
  const evidenceMap = useMemo(() => {
    const m = new Map<string, EvidenceRefDTO>()
    for (const ref of view.evidence_refs || []) {
      m.set(ref.id, ref)
    }
    return m
  }, [view.evidence_refs])

  const decisionInfo =
    DECISION_LABEL_MAP[view.decision.label] || {
      text: view.decision.label,
      color: 'default',
    }

  return (
    <>
      <Tabs
        activeKey={currentRole}
        onChange={(k) => onRoleChange(k as ScriptViewRole)}
        items={ROLE_TABS.map((t) => ({
          key: t.key,
          label: (
            <Tooltip title={t.description}>
              <span>{t.label}</span>
            </Tooltip>
          ),
        }))}
        className={styles.roleTabs}
      />

      <Card variant="borderless" className={styles.decisionCard}>
        <Row gutter={[24, 16]} align="middle">
          <Col xs={24} md={16}>
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Space size={8} style={{ width: '100%', justifyContent: 'space-between' }}>
                <Space size={8}>
                  <Tag color={decisionInfo.color} className={styles.decisionTag}>
                    {decisionInfo.text}
                  </Tag>
                  <Tag color="blue">
                    {CONFIDENCE_LABEL[view.decision.confidence] || view.decision.confidence}
                  </Tag>
                </Space>
                <FeedbackButton
                  scriptId={scriptId}
                  scope="general"
                  contextLabel={`整体决策「${decisionInfo.text}」`}
                />
              </Space>
              <Title level={4} className={styles.decisionReason}>
                {view.decision.one_sentence_reason}
              </Title>
              {view.summary ? (
                <Paragraph type="secondary" className={styles.summary}>
                  {view.summary}
                </Paragraph>
              ) : null}
              {view.role_focus.length > 0 ? (
                <Space size={6} wrap>
                  <Text type="secondary">该视角优先关注：</Text>
                  {view.role_focus.map((d) => (
                    <Tag key={d} color="purple">
                      {DIMENSION_META[d]?.label || d}
                    </Tag>
                  ))}
                </Space>
              ) : null}
            </Space>
          </Col>
          <Col xs={24} md={8}>
            <OverallScore score={view.overall_score} />
          </Col>
        </Row>
      </Card>

      <section className={styles.section}>
        <Title level={4}>5 维评分</Title>
        <Row gutter={[16, 16]}>
          {view.scorecard.map((item) => (
            <Col xs={24} sm={12} md={8} lg={8} xl={Math.floor(24 / view.scorecard.length) || 5} key={item.dimension}>
              <ScorecardCard scriptId={scriptId} item={item} evidenceMap={evidenceMap} />
            </Col>
          ))}
        </Row>
      </section>

      <Row gutter={[24, 24]} className={styles.section}>
        <Col xs={24} lg={14}>
          <Card title={<span>必读场景（Top {view.must_read_scene_ids.length}）</span>}>
            {view.must_read_scene_ids.length === 0 ? (
              <Empty description="该视角没有必读场景" />
            ) : (
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                {view.must_read_scene_ids.map((rid) => {
                  const ref = evidenceMap.get(rid)
                  if (!ref) {
                    return (
                      <Alert
                        key={rid}
                        type="warning"
                        message={`evidence_ref_id=${rid} 在 evidence_refs 中找不到`}
                      />
                    )
                  }
                  return <MustReadItem key={rid} scriptId={scriptId} ref_={ref} />
                })}
              </Space>
            )}
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title="审核风险标记">
            {view.risk_flags.length === 0 ? (
              <Empty description="未发现风险" />
            ) : (
              <Space wrap size={8}>
                {view.risk_flags.map((f) => (
                  <Tag key={f} color="red">
                    {f}
                  </Tag>
                ))}
              </Space>
            )}
          </Card>
        </Col>
      </Row>
    </>
  )
}

// ====================== 子组件 ======================

function OverallScore({ score }: { score: number | null }) {
  if (score === null || score === undefined) {
    return (
      <Card variant="borderless" className={styles.overallCard}>
        <Text type="secondary">综合评分</Text>
        <div className={styles.overallNull}>未给分</div>
        <Text type="secondary" style={{ fontSize: 12 }}>
          ≥3 维证据不足（rubric §6）
        </Text>
      </Card>
    )
  }
  const pct = Math.round((score / 10) * 100)
  const color = score >= 7 ? '#52c41a' : score >= 5 ? '#faad14' : '#ff4d4f'
  return (
    <Card variant="borderless" className={styles.overallCard}>
      <Text type="secondary">综合评分</Text>
      <Progress
        type="dashboard"
        percent={pct}
        format={() => (
          <span style={{ color }}>
            {score.toFixed(1)}
            <span style={{ fontSize: 14, color: 'rgba(0,0,0,0.45)' }}>/10</span>
          </span>
        )}
        strokeColor={color}
      />
    </Card>
  )
}

function ScorecardCard({
  scriptId,
  item,
  evidenceMap,
}: {
  scriptId: string
  item: ScorecardItemDTO
  evidenceMap: Map<string, EvidenceRefDTO>
}) {
  const meta = DIMENSION_META[item.dimension] || {
    key: item.dimension,
    label: item.dimension,
    shortLabel: item.dimension,
    hint: '',
  }
  const evidenceCount = item.evidence_ref_ids.length

  return (
    <Card className={styles.dimCard} variant="outlined">
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Tooltip title={meta.hint}>
            <Text strong className={styles.dimName}>
              {meta.label}
            </Text>
          </Tooltip>
          {item.level ? (
            <Tag color={LEVEL_COLOR[item.level] || 'default'}>
              {LEVEL_LABEL[item.level] || item.level}
            </Tag>
          ) : (
            <Tag>未给分</Tag>
          )}
        </Space>
        {item.score === null || item.score === undefined ? (
          <div className={styles.dimScoreNull}>—</div>
        ) : (
          <div className={styles.dimScore}>
            {item.score}
            <span className={styles.dimScoreUnit}>/10</span>
          </div>
        )}
        <Paragraph className={styles.dimReason} ellipsis={{ rows: 4, expandable: true, symbol: '展开' }}>
          {item.reason}
        </Paragraph>
        <Space size={6} wrap style={{ width: '100%', justifyContent: 'space-between' }}>
          <Space size={6} wrap>
            <Tag>{evidenceCount} 条证据</Tag>
            {item.evidence_ref_ids.slice(0, 2).map((rid) => {
              const ref = evidenceMap.get(rid)
              return ref ? (
                <Tooltip key={rid} title={ref.quote}>
                  <Tag color="geekblue">
                    {ref.scene_label || ref.scene_no || ref.scene_id.slice(0, 6)}
                  </Tag>
                </Tooltip>
              ) : null
            })}
          </Space>
          <FeedbackButton
            scriptId={scriptId}
            scope="dimension"
            scopeRef={item.dimension}
            contextLabel={`${meta.label} 评分`}
          />
        </Space>
      </Space>
    </Card>
  )
}

function MustReadItem({ scriptId, ref_ }: { scriptId: string; ref_: EvidenceRefDTO }) {
  const sceneLabel = ref_.scene_label || ref_.scene_no || ref_.scene_id.slice(0, 8)
  return (
    <div className={styles.mustReadItem}>
      <Space size={8} className={styles.mustReadHeader} style={{ width: '100%', justifyContent: 'space-between' }}>
        <Space size={8}>
          <Tag color="purple">{sceneLabel}</Tag>
          {ref_.confidence ? (
            <Tag>{CONFIDENCE_LABEL[ref_.confidence] || ref_.confidence}</Tag>
          ) : null}
        </Space>
        <FeedbackButton
          scriptId={scriptId}
          scope="scene"
          scopeRef={ref_.scene_id}
          contextLabel={`必读场景「${sceneLabel}」`}
        />
      </Space>
      <Paragraph className={styles.mustReadQuote} ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}>
        "{ref_.quote}"
      </Paragraph>
      <Text type="secondary" className={styles.mustReadReason}>
        {ref_.reason}
      </Text>
    </div>
  )
}
