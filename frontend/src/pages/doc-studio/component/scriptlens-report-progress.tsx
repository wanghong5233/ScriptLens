/**
 * ScriptLens · 5 维评分流水线进度面板。
 *
 * 设计意图（与 ScholarMind DeepResearch 区别开）：
 *   - DeepResearch 是动态分支型（N 个 block 并发，需要 Timeline 实时事件流 + 证据链定位）。
 *   - ScriptLens 评分是固定 6 阶段流水线，每步语义明确；用户只需要"我现在在哪一步、
 *     这一步在做什么、还要多久"三个问题的答案，不需要细到每条 LLM call 的事件流。
 *   - 因此用「6 阶段竖向时间轴 + 当前阶段 detail + 已耗时」的轻量化形态，配莫兰迪奶油色，
 *     与 doc-studio 主题一致。
 *
 * 数据流：
 *   - 进入面板时立即拉一次 GET /scripts/{id}/progress；
 *   - snapshot 非空 → 渲染 6 阶段；轮询 1.5s 拉一次。
 *   - snapshot 为空 → 表示 tracker 没记录这个 script_id 的进度。两种可能：
 *       a) 历史剧本（status=ready 但从未跑过评分）—— 此时上传链路里的 auto reanalyze
 *          没机会跑（剧本是早就 ingest 完的），需要前端主动触发一次。
 *       b) 后端进程重启 / GC 了旧快照。
 *     处理：第一次发现 snapshot=null 时调 onAutoTrigger（=父组件的 reanalyze），下一轮
 *     轮询就能拿到 tracker.start 写进去的 6 阶段。设 ref 去重，避免反复触发。
 *   - snapshot.final=true 即流水线结束（成功或失败），停止轮询；外层组件根据 reports
 *     表是否有数据决定切到 ready 还是显示错误。
 */

import { CheckCircleFilled, ExclamationCircleFilled, LoadingOutlined } from '@ant-design/icons'
import { Button, Spin, Tooltip, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import {
  fetchScriptReportProgress,
  type ReportProgressSnapshotDTO,
  type ReportStageDTO,
  type ReportStageState,
} from '@/api/docStudio'
import styles from './scriptlens-report-progress.module.scss'

const { Text } = Typography

const POLL_INTERVAL_MS = 1500

interface Props {
  scriptId: string
  /** 上层（rail / report 页）需要在 final=true 时刷一次报告，由它决定切换到 ready 还是失败态 */
  onFinalized?: (snapshot: ReportProgressSnapshotDTO) => void
  /** snapshot 首次为 null 时被调一次 —— 通常是父组件的 reanalyze（历史剧本/重启后无快照场景） */
  onAutoTrigger?: () => Promise<void> | void
  /** 上层兜底：自动触发后仍长期拿不到 snapshot 时展示的节点（手动重跑按钮） */
  fallback?: React.ReactNode
  /** 紧凑模式（右栏窄）：隐藏 description，只保留 label + detail */
  compact?: boolean
}

// snapshot=null 持续多久后让 fallback 接手（避免用户对着空 spinner 干等）
const FALLBACK_GRACE_MS = 12_000

export default function ScriptlensReportProgress({
  scriptId,
  onFinalized,
  onAutoTrigger,
  fallback,
  compact = false,
}: Props) {
  const [snapshot, setSnapshot] = useState<ReportProgressSnapshotDTO | null>(null)
  const [loading, setLoading] = useState(true)
  const [now, setNow] = useState(() => Date.now() / 1000)
  const pollTimerRef = useRef<number | null>(null)
  const finalizedNotifiedRef = useRef(false)
  const autoTriggeredRef = useRef(false)
  const nullCountRef = useRef(0)
  const firstNullAtRef = useRef<number | null>(null)
  const [autoTriggerFailed, setAutoTriggerFailed] = useState(false)

  // 把外部 callback 装进 ref：父组件每次 render 都会传新闭包，但 useEffect 只
  // 在 scriptId 变化时重启，避免 autoTriggeredRef 等被反复重置导致重复触发。
  const onFinalizedRef = useRef(onFinalized)
  const onAutoTriggerRef = useRef(onAutoTrigger)
  useEffect(() => {
    onFinalizedRef.current = onFinalized
    onAutoTriggerRef.current = onAutoTrigger
  })

  useEffect(() => {
    // 切换 scriptId 才重置内部 ref / state；同 scriptId 期间外部 callback 闭包变化不影响
    finalizedNotifiedRef.current = false
    autoTriggeredRef.current = false
    nullCountRef.current = 0
    firstNullAtRef.current = null
    setAutoTriggerFailed(false)
    setSnapshot(null)
    setLoading(true)

    let cancelled = false

    const stop = () => {
      if (pollTimerRef.current !== null) {
        window.clearTimeout(pollTimerRef.current)
        pollTimerRef.current = null
      }
    }

    const tick = async () => {
      if (cancelled) return
      let snap: ReportProgressSnapshotDTO | null = null
      try {
        const resp = await fetchScriptReportProgress(scriptId)
        snap = resp.snapshot
      } catch {
        // 进度接口失败不影响主流程，保留上一次 snapshot 继续轮
      }
      if (cancelled) return

      setSnapshot(snap)
      setLoading(false)

      if (snap?.final && !finalizedNotifiedRef.current) {
        finalizedNotifiedRef.current = true
        onFinalizedRef.current?.(snap)
      }

      if (snap == null) {
        nullCountRef.current += 1
        if (firstNullAtRef.current == null) {
          firstNullAtRef.current = Date.now()
        }
        // 第一次 null 不触发：可能是上传链路刚 schedule 的 BackgroundTask 还没切到
        // tracker.start（< 1s 时间窗）。第二次仍 null（即 ≥ 1.5s 后）才触发，
        // 既给后端 task 上车机会，也避免和后端正在跑的 generate_report 撞车。
        if (
          nullCountRef.current >= 2 &&
          !autoTriggeredRef.current &&
          onAutoTriggerRef.current
        ) {
          autoTriggeredRef.current = true
          try {
            await onAutoTriggerRef.current()
          } catch {
            if (!cancelled) setAutoTriggerFailed(true)
          }
        }
      } else {
        nullCountRef.current = 0
        firstNullAtRef.current = null
      }

      if (cancelled) return
      // final=true 后停止轮询；外层 onFinalized 回调已在上面触发刷新
      if (snap?.final) return
      pollTimerRef.current = window.setTimeout(tick, POLL_INTERVAL_MS)
    }

    void tick()
    return () => {
      cancelled = true
      stop()
    }
  }, [scriptId])

  // 显示已耗时用，1s tick
  useEffect(() => {
    if (!snapshot || snapshot.final) return
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => window.clearInterval(timer)
  }, [snapshot?.started_at, snapshot?.final])

  if (loading && !snapshot) {
    return (
      <div className={styles.progress}>
        <div className={styles.fallbackCenter}>
          <Spin indicator={<LoadingOutlined style={{ fontSize: 24 }} spin />} />
          <Text type="secondary" style={{ marginTop: 12, fontSize: 12 }}>
            正在连接评分流水线…
          </Text>
        </div>
      </div>
    )
  }

  if (!snapshot) {
    // snapshot 为 null 的渐进策略：
    //   1. 自动触发失败 → 立刻给 fallback（避免无限转圈）
    //   2. 自动触发已发出，但 tracker 还没写第一帧（reanalyze 202 → BackgroundTask 排队
    //      → tracker.start 之间有 < 2s 间隔）→ 显示"正在拉起评分流水线"过渡态
    //   3. 12s 后 tracker 仍空 → 让 fallback 接手（通常是后端拒绝了 reanalyze，e.g. 409）
    const ms = firstNullAtRef.current ? Date.now() - firstNullAtRef.current : 0
    if (autoTriggerFailed || ms > FALLBACK_GRACE_MS) {
      return <>{fallback}</>
    }
    return (
      <div className={styles.progress}>
        <div className={styles.fallbackCenter}>
          <Spin indicator={<LoadingOutlined style={{ fontSize: 24 }} spin />} />
          <Text style={{ marginTop: 12, fontSize: 13, fontWeight: 500 }}>
            正在拉起评分流水线…
          </Text>
          <Text type="secondary" style={{ marginTop: 4, fontSize: 12, textAlign: 'center' }}>
            刚刚已自动触发整剧 5 维分析，
            <br />
            后台正在排队启动，1~2 秒后会出现 6 阶段进度。
          </Text>
        </div>
      </div>
    )
  }

  const totalElapsedSec = Math.max(0, Math.floor(now - snapshot.started_at))
  const stageCount = snapshot.stages.length
  const doneCount = snapshot.stages.filter((s) => s.state === 'done').length
  const failedStage = snapshot.stages.find((s) => s.state === 'failed')

  // 顶部状态文本
  const headerHint = (() => {
    if (snapshot.error || failedStage) {
      return failedStage
        ? `第 ${snapshot.current_index + 1} 步失败：${failedStage.label}`
        : '评分流水线异常'
    }
    if (snapshot.final) {
      return `已完成 · 共 ${stageCount} 步`
    }
    const cur = snapshot.stages[snapshot.current_index]
    return cur ? `步骤 ${snapshot.current_index + 1} / ${stageCount} · ${cur.label}` : ''
  })()

  return (
    <div className={styles.progress}>
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <span className={styles.headerTitle}>
            {snapshot.error ? '评分异常' : snapshot.final ? '评分完成' : '正在生成 5 维分析报告'}
          </span>
          <span className={styles.headerSubtitle}>{headerHint}</span>
        </div>
        <div className={styles.headerRight}>
          <span className={styles.headerTime} title="已耗时">
            {formatDuration(totalElapsedSec)}
          </span>
          <span className={styles.headerProgress}>
            {doneCount} / {stageCount}
          </span>
        </div>
      </div>

      {snapshot.error ? (
        <div className={styles.errorBox}>
          <ExclamationCircleFilled style={{ color: '#C97A7A', marginRight: 6 }} />
          <Text style={{ fontSize: 12, color: '#8A4A4A' }}>{snapshot.error}</Text>
        </div>
      ) : null}

      <ul className={styles.stageList}>
        {snapshot.stages.map((stage, index) => {
          const isCurrent =
            index === snapshot.current_index && stage.state === 'running'
          return (
            <StageRow
              key={stage.id}
              stage={stage}
              isCurrent={isCurrent}
              isLast={index === stageCount - 1}
              now={now}
              compact={compact}
            />
          )
        })}
      </ul>

      <div className={styles.scopeHint}>
        <Text type="secondary" style={{ fontSize: 11.5 }}>
          分析对象是<strong>整部剧本</strong>（非当前场次）。
          中间编辑区切到具体某场后，可在右栏「Agent 对话」就该场提问。
        </Text>
      </div>
    </div>
  )
}

interface StageRowProps {
  stage: ReportStageDTO
  isCurrent: boolean
  isLast: boolean
  now: number
  compact: boolean
}

function StageRow({ stage, isCurrent, isLast, now, compact }: StageRowProps) {
  const elapsed = stageElapsed(stage, now)

  return (
    <li className={`${styles.stage} ${styles[`stage--${stage.state}`]}`}>
      <div className={styles.stageRail}>
        <StageDot state={stage.state} isCurrent={isCurrent} />
        {isLast ? null : <span className={styles.stageLine} />}
      </div>
      <div className={styles.stageBody}>
        <div className={styles.stageHeadline}>
          <Tooltip title={compact ? stage.description : undefined} placement="topLeft">
            <span className={styles.stageLabel}>{stage.label}</span>
          </Tooltip>
          {elapsed != null ? (
            <span className={styles.stageElapsed}>{formatDuration(elapsed)}</span>
          ) : null}
        </div>
        {!compact ? (
          <div className={styles.stageDescription}>{stage.description}</div>
        ) : null}
        {stage.detail ? (
          <div className={styles.stageDetail}>
            {isCurrent ? <span className={styles.stageDetailDot} /> : null}
            {stage.detail}
          </div>
        ) : null}
      </div>
    </li>
  )
}

function StageDot({ state, isCurrent }: { state: ReportStageState; isCurrent: boolean }) {
  if (state === 'done') {
    return <CheckCircleFilled className={`${styles.dot} ${styles['dot--done']}`} />
  }
  if (state === 'failed') {
    return (
      <ExclamationCircleFilled className={`${styles.dot} ${styles['dot--failed']}`} />
    )
  }
  if (state === 'running' || isCurrent) {
    return (
      <span className={`${styles.dot} ${styles['dot--running']}`}>
        <span className={styles.dotPulse} />
        <span className={styles.dotCore} />
      </span>
    )
  }
  return <span className={`${styles.dot} ${styles['dot--pending']}`} />
}

function stageElapsed(stage: ReportStageDTO, now: number): number | null {
  if (stage.state === 'pending' || stage.started_at == null) return null
  const end = stage.completed_at ?? now
  return Math.max(0, Math.floor(end - stage.started_at))
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0s'
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${m}m ${s.toString().padStart(2, '0')}s`
}

/** 兜底用：包一个手动重跑按钮（外层使用时传给 fallback 属性即可）。 */
export function ProgressFallbackPanel({
  reanalyzing,
  onReanalyze,
}: {
  reanalyzing: boolean
  onReanalyze: () => void
}) {
  return (
    <div className={styles.fallbackCenter}>
      <Spin indicator={<LoadingOutlined style={{ fontSize: 26 }} spin />} />
      <Text style={{ marginTop: 12, fontSize: 13, fontWeight: 500 }}>
        正在自动生成 5 维分析报告
      </Text>
      <Text type="secondary" style={{ marginTop: 4, fontSize: 12, textAlign: 'center' }}>
        若长时间未出现进度，可手动重跑（覆盖旧报告）
      </Text>
      <Button
        size="small"
        loading={reanalyzing}
        onClick={onReanalyze}
        style={{ marginTop: 12 }}
      >
        手动重跑评分
      </Button>
    </div>
  )
}
