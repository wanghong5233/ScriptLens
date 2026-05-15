/**
 * RewritePlanCard —— chat 流里渲染全剧改写 plan 输出的
 * 全剧改写计划。
 *
 * 数据契约（与后端 rewrite_chain.RewritePlan.to_dict 对齐）：
 *   {
 *     dimensions: ["story", "emotion", ...],
 *     overall_summary: "≤120 字整体改写思路",
 *     steps: [
 *       {
 *         scene_id, scene_label, episode_no, scene_no,
 *         target_dimensions: ["story"],
 *         rationale: "为什么改这场 ≤80 字",
 *         expected_changes: "具体改什么 ≤120 字",
 *         current_excerpt: "改前节选 ≤200 字"
 *       }, ...
 *     ]
 *   }
 *
 * 业内对照（Cursor Composer plan / Copilot Workspace plan card / Devin plan view）：
 *   plan tree 必须 review-then-execute——勾选子集 → 执行；不允许 LLM 直接改场。
 */

import { useMemo, useState } from 'react'
import { Button, Checkbox, Space, Tag, Tooltip, Typography, Empty } from 'antd'
import { ThunderboltOutlined, EyeOutlined } from '@ant-design/icons'
import {
  formatSceneLocator,
  type AgentTask,
  type DimensionKey,
  type FulltextRewritePlanStep,
} from './agentTask'

const { Text, Paragraph } = Typography

const DIM_LABEL: Record<DimensionKey, string> = {
  story: '故事力',
  character: '人物力',
  concept: '题材力',
  emotion: '情感力',
  pacing: '叙事力',
}

export interface RewritePlanStepData {
  scene_id: string
  scene_label?: string
  episode_no?: number | null
  scene_no?: string | null
  target_dimensions: string[]
  rationale?: string
  expected_changes?: string
  current_excerpt?: string
}

export interface RewritePlanData {
  dimensions: string[]
  overall_summary?: string
  steps: RewritePlanStepData[]
}

interface Props {
  plan: RewritePlanData
  /** 是否已经派发过一次 execute（防止用户重复点击 / 重新审 plan 时禁用按钮） */
  executed?: boolean
  /** 跳到目标场原文（编辑器持久高亮，不派 Agent，与 traceEvidence 同语义） */
  onTraceScene: (sceneId: string) => void
  /** 派发 fulltext_rewrite execute task（autoSubmit 由父级在 dispatchAgentTask 处控制） */
  onDispatchExecute: (task: AgentTask) => void
}

export function RewritePlanCard({
  plan,
  executed,
  onTraceScene,
  onDispatchExecute,
}: Props) {
  const dims = useMemo<DimensionKey[]>(
    () => (plan.dimensions || []).filter(isDimensionKey),
    [plan.dimensions],
  )
  const steps = plan.steps || []

  // 默认全勾选（plan 已经被 LLM 收敛过，不必让用户做减法）
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(steps.map((s) => s.scene_id)),
  )

  const toggleAll = () => {
    if (selected.size === steps.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(steps.map((s) => s.scene_id)))
    }
  }

  const toggleOne = (sceneId: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(sceneId)) next.delete(sceneId)
      else next.add(sceneId)
      return next
    })
  }

  const handleExecute = () => {
    const chosenSteps: FulltextRewritePlanStep[] = []
    for (const s of steps) {
      if (!selected.has(s.scene_id)) continue
      const stepDims = (s.target_dimensions || []).filter(isDimensionKey)
      if (stepDims.length === 0) continue
      chosenSteps.push({
        scene_id: s.scene_id,
        target_dimensions: stepDims,
        expected_changes: s.expected_changes || undefined,
        scene_label: s.scene_label || undefined,
      })
    }
    if (chosenSteps.length === 0) return

    onDispatchExecute({
      kind: 'fulltext_rewrite',
      mode: 'execute',
      dimensions: dims,
      plan_steps: chosenSteps,
    })
  }

  if (steps.length === 0) {
    return (
      <div className="rewrite-plan-card rewrite-plan-card--empty">
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <Text type="secondary">
              {plan.overall_summary || '当前剧本在所选维度上没有 score < 7 的明显短板场，无须 plan-level 改写。'}
            </Text>
          }
        />
      </div>
    )
  }

  const allSelected = selected.size === steps.length && steps.length > 0

  return (
    <div className="rewrite-plan-card">
      <div className="rewrite-plan-card__header">
        <Space size={6} wrap>
          <ThunderboltOutlined className="rewrite-plan-card__icon" />
          <Text strong className="rewrite-plan-card__title">全剧改写计划</Text>
          {dims.map((d) => (
            <Tag key={d} color="gold" className="rewrite-plan-card__dim-tag">
              {DIM_LABEL[d]}
            </Tag>
          ))}
        </Space>
        <Text type="secondary" className="rewrite-plan-card__count">
          共 {steps.length} 场建议改写
        </Text>
      </div>

      {plan.overall_summary ? (
        <Paragraph className="rewrite-plan-card__summary">
          {plan.overall_summary}
        </Paragraph>
      ) : null}

      <div className="rewrite-plan-card__toolbar">
        <Checkbox
          indeterminate={selected.size > 0 && !allSelected}
          checked={allSelected}
          onChange={toggleAll}
        >
          {allSelected ? '取消全选' : '全选'}（已选 {selected.size} / {steps.length}）
        </Checkbox>
      </div>

      <div className="rewrite-plan-card__steps">
        {steps.map((step) => {
          const stepDims = (step.target_dimensions || []).filter(isDimensionKey)
          const locator =
            formatSceneLocator(step.episode_no ?? null, step.scene_no ?? null, step.scene_label ?? null) ||
            step.scene_label ||
            step.scene_id.slice(0, 8)
          const isChecked = selected.has(step.scene_id)
          return (
            <div
              key={step.scene_id}
              className={`rewrite-plan-card__step ${isChecked ? '' : 'rewrite-plan-card__step--off'}`}
            >
              <div className="rewrite-plan-card__step-head">
                <Checkbox
                  checked={isChecked}
                  onChange={() => toggleOne(step.scene_id)}
                  className="rewrite-plan-card__step-check"
                >
                  <Space size={6} wrap>
                    <Text strong className="rewrite-plan-card__step-locator">{locator}</Text>
                    {stepDims.map((d) => (
                      <Tag key={d} color="orange" className="rewrite-plan-card__step-dim">
                        {DIM_LABEL[d]}
                      </Tag>
                    ))}
                  </Space>
                </Checkbox>
                <Tooltip title="先看原文">
                  <Button
                    size="small"
                    type="text"
                    icon={<EyeOutlined />}
                    onClick={() => onTraceScene(step.scene_id)}
                  />
                </Tooltip>
              </div>
              {step.rationale ? (
                <div className="rewrite-plan-card__step-rationale">
                  <Text type="secondary" className="rewrite-plan-card__step-label">为什么改</Text>
                  <Text className="rewrite-plan-card__step-text">{step.rationale}</Text>
                </div>
              ) : null}
              {step.expected_changes ? (
                <div className="rewrite-plan-card__step-rationale">
                  <Text type="secondary" className="rewrite-plan-card__step-label">改什么</Text>
                  <Text className="rewrite-plan-card__step-text">{step.expected_changes}</Text>
                </div>
              ) : null}
              {step.current_excerpt ? (
                <div className="rewrite-plan-card__step-excerpt">
                  <Text type="secondary" className="rewrite-plan-card__step-label">改前节选</Text>
                  <Text className="rewrite-plan-card__step-excerpt-text">
                    {step.current_excerpt}
                  </Text>
                </div>
              ) : null}
            </div>
          )
        })}
      </div>

      <div className="rewrite-plan-card__actions">
        <Button
          type="primary"
          icon={<ThunderboltOutlined />}
          onClick={handleExecute}
          disabled={selected.size === 0 || executed}
        >
          {executed ? '已执行' : `执行选中（${selected.size} 场）`}
        </Button>
        <Text type="secondary" className="rewrite-plan-card__actions-hint">
        </Text>
      </div>
    </div>
  )
}

function isDimensionKey(d: any): d is DimensionKey {
  return d === 'story' || d === 'character' || d === 'concept' || d === 'emotion' || d === 'pacing'
}
