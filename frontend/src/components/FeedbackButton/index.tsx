import { useState } from 'react'
import { Button, Input, message, Popover, Space, Tooltip } from 'antd'
import { DislikeOutlined, LikeOutlined, CheckOutlined } from '@ant-design/icons'
import {
  submitScriptFeedback,
  type ScriptFeedbackScope,
} from '@/api/docStudio'
import styles from './index.module.scss'

const { TextArea } = Input

type Rating = 'up' | 'down'

export interface FeedbackButtonProps {
  scriptId: string
  scope: ScriptFeedbackScope
  scopeRef?: string | null
  /** 给反馈正文做人类可读上下文，如 "opening_hook 评分" / "决策卡" / "必读场景 1-3" */
  contextLabel: string
  size?: 'small' | 'middle'
  /** 一个 FeedbackButton 组提交后整个组进入 disabled / "已反馈" 态 */
  onSubmitted?: (rating: Rating) => void
}

/**
 * 维度卡 / 决策卡 / 必读场景的好坏反馈按钮组（PRD §10 P3）。
 *
 * 交互：[👍] [👎] → Popover 让用户填可选 comment → 提交 → 整组变"已反馈"。
 * 后端落库后，下次 chat 会把最近 N 条反馈注入 system prompt（轻量 skill 机制）。
 */
export default function FeedbackButton(props: FeedbackButtonProps) {
  const { scriptId, scope, scopeRef, contextLabel, size = 'small', onSubmitted } = props
  const [submittedAs, setSubmittedAs] = useState<Rating | null>(null)
  const [openRating, setOpenRating] = useState<Rating | null>(null)
  const [comment, setComment] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async (rating: Rating) => {
    setSubmitting(true)
    try {
      const tag = rating === 'up' ? '[👍]' : '[👎]'
      const trimmed = comment.trim()
      const messageBody = trimmed
        ? `${tag} ${trimmed}`
        : `${tag} 来自 ${contextLabel}（无评论）`
      await submitScriptFeedback(scriptId, {
        scope,
        scope_ref: scopeRef ?? null,
        message: messageBody,
      })
      setSubmittedAs(rating)
      setOpenRating(null)
      setComment('')
      message.success('反馈已记录，下次对话 Agent 会感知你的偏好')
      onSubmitted?.(rating)
    } catch (err: unknown) {
      const e = err as { response?: { data?: { detail?: string } }; message?: string }
      message.error(`提交失败：${e?.response?.data?.detail || e?.message || '未知错误'}`)
    } finally {
      setSubmitting(false)
    }
  }

  if (submittedAs) {
    return (
      <Tooltip title={`已反馈：${submittedAs === 'up' ? '👍' : '👎'}（${contextLabel}）`}>
        <Button
          type="text"
          size={size}
          icon={
            submittedAs === 'up' ? (
              <LikeOutlined style={{ color: '#52c41a' }} />
            ) : (
              <DislikeOutlined style={{ color: '#ff4d4f' }} />
            )
          }
          disabled
          className={styles.submittedBtn}
        >
          已反馈
        </Button>
      </Tooltip>
    )
  }

  const renderPopoverContent = (rating: Rating) => (
    <div className={styles.popContent}>
      <div className={styles.popTitle}>
        {rating === 'up' ? '👍 ' : '👎 '}
        反馈：{contextLabel}
      </div>
      <TextArea
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        placeholder={
          rating === 'up'
            ? '哪里做得好？（选填，<= 200 字）'
            : '哪里需要改进？（选填，<= 200 字）'
        }
        maxLength={200}
        autoSize={{ minRows: 2, maxRows: 4 }}
        showCount
      />
      <Space className={styles.popActions}>
        <Button
          size="small"
          onClick={() => {
            setOpenRating(null)
            setComment('')
          }}
        >
          取消
        </Button>
        <Button
          size="small"
          type="primary"
          icon={<CheckOutlined />}
          loading={submitting}
          onClick={() => handleSubmit(rating)}
        >
          提交
        </Button>
      </Space>
    </div>
  )

  return (
    <Space size={4}>
      <Popover
        content={renderPopoverContent('up')}
        trigger="click"
        open={openRating === 'up'}
        onOpenChange={(open) => {
          setOpenRating(open ? 'up' : null)
          if (!open) setComment('')
        }}
        destroyTooltipOnHide
      >
        <Tooltip title="这个判断有用">
          <Button type="text" size={size} icon={<LikeOutlined />} />
        </Tooltip>
      </Popover>
      <Popover
        content={renderPopoverContent('down')}
        trigger="click"
        open={openRating === 'down'}
        onOpenChange={(open) => {
          setOpenRating(open ? 'down' : null)
          if (!open) setComment('')
        }}
        destroyTooltipOnHide
      >
        <Tooltip title="这个判断有问题">
          <Button type="text" size={size} icon={<DislikeOutlined />} />
        </Tooltip>
      </Popover>
    </Space>
  )
}
