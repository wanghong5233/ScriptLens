/**
 * Doc Studio 空态水印引导：无工作区/未打开文件时展示。
 * 展示当前支持的快捷键与产品亮点（基于 ReAct Agent 架构文档）。
 */
import {
  ApiOutlined,
  CopyOutlined,
  SafetyOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  ToolOutlined,
} from '@ant-design/icons'
import classNames from 'classnames'
import styles from './doc-studio-welcome.module.scss'

const SHORTCUTS = [
  { keys: 'Ctrl+B', desc: '折叠/展开文件栏' },
  { keys: 'Ctrl+L', desc: '展开对话、引用选区到指令' },
  { keys: 'Ctrl+S', desc: '保存' },
  { keys: 'Ctrl+Enter', desc: '发送指令' },
  { keys: 'Ctrl+V', desc: '粘贴图片（对话输入框）' },
  { keys: 'F2', desc: '重命名文件/文件夹' },
] as const

const HIGHLIGHTS = [
  {
    icon: <ApiOutlined />,
    label: 'Ask/Agent 双模式',
    text: 'Ask 只读分析，Agent 全工具执行，ReAct 推理-行动循环',
  },
  {
    icon: <ToolOutlined />,
    label: '动态工具编排',
    text: '定位→读片段→精确改写，16 种工具独立预算守卫',
  },
  {
    icon: <SafetyOutlined />,
    label: 'Human-in-the-loop',
    text: '危险操作（如批量删除）需用户确认后再执行',
  },
  {
    icon: <SearchOutlined />,
    label: '语义混合检索',
    text: 'embedding + lexical n-gram，增量索引与冷启动预热',
  },
  {
    icon: <ThunderboltOutlined />,
    label: '异步可取消',
    text: 'SSE 全链路事件，支持中断与断线重连回放',
  },
  {
    icon: <CopyOutlined />,
    label: '多模态',
    text: '图片附件自动切换 vision 模型',
  },
] as const

export default function DocStudioWelcome() {
  return (
    <div className={classNames(styles['doc-studio-welcome'])}>
      <div className={styles['doc-studio-welcome__title']}>请先选择工作区</div>
      <div className={styles['doc-studio-welcome__tagline']}>
        体验参考 Cursor，侧重 LaTeX/Markdown 等文档的智能编辑
      </div>
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
