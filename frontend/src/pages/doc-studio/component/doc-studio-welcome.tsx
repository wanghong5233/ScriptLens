/**
 * ScriptLens · Doc Studio 空态欢迎页：未选剧本或新剧本未打开任何场次时展示。
 * 复用 ScholarMind doc-studio-welcome 模板（6 项产品亮点 + 5 条快捷键），文案剧本场景化。
 *
 * 设计原则（PRD §5、§7、§10）：
 *   - 用户首次见到产品时 5 秒内能 get「这是什么 + 我能做什么」
 *   - 不只放 CTA，把核心能力（评分 / 证据 / 改写 / 反馈 / 三视角 / 联网）一次铺出来
 *   - 快捷键提示和实际编辑器行为一致，与 doc-studio 其他页面同步
 */
import {
  AimOutlined,
  ApiOutlined,
  CloudUploadOutlined,
  EditOutlined,
  GlobalOutlined,
  SafetyOutlined,
  TeamOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { Button } from 'antd'
import classNames from 'classnames'
import styles from './doc-studio-welcome.module.scss'

interface DocStudioWelcomeProps {
  /**
   * 中央 CTA「上传剧本」按钮回调。建议接到 setWorkspaceModalOpen(true)，
   * 复用 index.tsx 中已有的「上传剧本」Modal。可选；不传时按钮隐藏。
   */
  onUploadClick?: () => void
}

const SHORTCUTS = [
  { keys: 'Ctrl+B', desc: '折叠 / 展开左侧场次栏' },
  { keys: 'Ctrl+L', desc: '展开 Agent 对话、把选区作为追问上下文' },
  { keys: 'Ctrl+I', desc: '在选区或光标处发起改写 / 追问' },
  { keys: 'Ctrl+Enter', desc: '发送对话指令' },
  { keys: 'Ctrl+S', desc: '保存当前场次的本地编辑' },
  { keys: 'F2', desc: '重命名剧本' },
] as const

const HIGHLIGHTS = [
  {
    icon: <AimOutlined />,
    label: '5 维评分 + 证据定位',
    text: '开场钩子 / 爽点密度 / 动机自洽 / 节奏 / 审核风险，每项分数都要原文锚点',
  },
  {
    icon: <TeamOutlined />,
    label: '三视角报告',
    text: '选品 / 编剧统筹 / 平台审核 一份剧本三种解读，前端纯重排不重新生成',
  },
  {
    icon: <EditOutlined />,
    label: '低分维度改写',
    text: '按 target_dimension 触发 LLM 改写建议，AgentDiffReview 逐 hunk Keep / Undo',
  },
  {
    icon: <ApiOutlined />,
    label: 'Ask / Agent 双模式',
    text: 'Ask 只读分析复盘，Agent 主动调工具 + ReAct 推理-行动循环',
  },
  {
    icon: <GlobalOutlined />,
    label: '联网检索兜底',
    text: '剧本之外的查询（市场 / 法规 / 同类爆款）走 web_search，结论必须列源 URL',
  },
  {
    icon: <SafetyOutlined />,
    label: 'Human-in-the-loop',
    text: '报告 / 维度 / 改写 / 场次任意位置可反馈，下次对话自动注入用户偏好',
  },
  {
    icon: <ThunderboltOutlined />,
    label: '异步 SSE 流式',
    text: '上传 → 解析 → 评分全链路事件流，断线 Last-Event-ID 重连续传',
  },
] as const

export default function DocStudioWelcome({ onUploadClick }: DocStudioWelcomeProps) {
  return (
    <div className={classNames(styles['doc-studio-welcome'])}>
      <div className={styles['doc-studio-welcome__title']}>ScriptLens · 短剧剧本智能分析</div>
      <div className={styles['doc-studio-welcome__tagline']}>
        上传一份完整短剧剧本（docx / pdf / txt），AI 自动切场、5 维评分、定位证据、给出改写建议
      </div>
      {onUploadClick ? (
        <Button
          type="primary"
          size="large"
          icon={<CloudUploadOutlined />}
          onClick={onUploadClick}
          className={styles['doc-studio-welcome__cta']}
        >
          上传剧本，开始分析
        </Button>
      ) : null}
      <div className={styles['doc-studio-welcome__sections']}>
        <section className={styles['doc-studio-welcome__section']}>
          <div className={styles['doc-studio-welcome__section-title']}>快捷键</div>
          <ul className={styles['doc-studio-welcome__shortcuts']}>
            {SHORTCUTS.map((s, idx) => (
              <li key={idx} className={styles['doc-studio-welcome__shortcut']}>
                <kbd>{s.keys}</kbd>
                <span>{s.desc}</span>
              </li>
            ))}
          </ul>
        </section>
        <section className={styles['doc-studio-welcome__section']}>
          <div className={styles['doc-studio-welcome__section-title']}>产品亮点</div>
          <ul className={styles['doc-studio-welcome__highlights']}>
            {HIGHLIGHTS.map((h, idx) => (
              <li key={idx} className={styles['doc-studio-welcome__highlight']}>
                <span className={styles['doc-studio-welcome__highlight-badge']}>{h.icon}</span>
                <div className={styles['doc-studio-welcome__highlight-content']}>
                  <strong>{h.label}</strong>
                  <span>：{h.text}</span>
                </div>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  )
}
