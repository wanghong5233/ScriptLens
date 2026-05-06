import classNames from 'classnames'
import ReactMarkdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import rehypeRaw from 'rehype-raw'
import { Tooltip } from 'antd'
import { useCallback, useMemo } from 'react'
import 'katex/dist/katex.min.css'
import './index.scss'
import { preprocessMarkdownMath } from './mathPreprocess'

type MarkdownProps = {
  className?: string
  value?: string
  onClick?: React.MouseEventHandler<HTMLDivElement>
  references?: API.Reference[]
  onCitationClick?: (index: number, reference: API.Reference) => void
}

// 工业级 RAG 引用契约（参考 Perplexity / NotebookLM / Anthropic Claude Citations）：
//   chip 数字 N  ↔  服务端 [Context] 第 N 项  ↔  references[N-1]（右侧引文卡片）
// 三方一致、1-based、N ∈ [1, K]。任何越界或异形格式都视为无引用，按原文显示。
//
// 实现要点：
// - 把 `[N]` 替换成自定义 HTML 标签 `<refchip data-n="N">N</refchip>`，
//   再交给 react-markdown，并在 components map 里注册自定义元素，渲染成
//   带 antd Tooltip 的可点击 pill。这样 chip 是真正的 React 节点，
//   能挂 onMouseEnter / onClick / Tooltip，比事件委托干净得多。
// - 通过 references 数量做越界校验，避免幻觉的 [99] 在没有第 99 条时
//   仍渲染成 chip 误导用户。
// - 屏蔽代码块 / 行内代码 / Markdown 链接，避免 `arr[1]`、`[label](url)`
//   被误命中。

function transformCitations(input: string, maxN: number): string {
  if (!input) return ''

  const placeholders: string[] = []
  const stash = (raw: string): string => {
    placeholders.push(raw)
    return `\u0000PLH${placeholders.length - 1}\u0000`
  }

  let masked = input.replace(/```[\s\S]*?```|`[^`\n]*`/g, (m) => stash(m))
  masked = masked.replace(/\[[^\]\n]*\]\([^)\n]*\)/g, (m) => stash(m))

  let result = masked.replace(
    /(^|[^\w(\[])\[(\d{1,3})\](?!\w)/g,
    (_, prefix: string, idx: string) => {
      const num = Number(idx)
      if (!Number.isFinite(num) || num <= 0 || num > maxN) {
        return `${prefix}[${idx}]`
      }
      return `${prefix}<refchip data-n="${num}">${num}</refchip>`
    },
  )

  result = result.replace(
    /\u0000PLH(\d+)\u0000/g,
    (_, i: string) => placeholders[Number(i)] ?? '',
  )
  return result
}

function buildPreviewTitle(ref: API.Reference): string {
  const docName = ref.document_title || ref.document_name || `引文 ${ref.id ?? ''}`
  const page = typeof ref.page === 'number' ? `· p${ref.page}` : ''
  return `${docName} ${page}`.trim()
}

function buildPreviewBody(ref: API.Reference): string {
  const text = (ref.snippet || ref.source_text || '').trim()
  if (!text) return '（无可展示的片段）'
  return text.length > 160 ? `${text.slice(0, 160)}…` : text
}

function MarkdownComponent({
  className,
  value,
  onClick,
  references,
  onCitationClick,
}: MarkdownProps) {
  const refs = references ?? []
  const maxN = refs.length

  const content = useMemo(() => {
    if (!value) return ''
    const normalized = preprocessMarkdownMath(value)
    return transformCitations(normalized, maxN)
  }, [value, maxN])

  const handleChipClick = useCallback(
    (n: number) => {
      const idx = n - 1
      const target = refs[idx]
      if (target) onCitationClick?.(idx, target)
    },
    [refs, onCitationClick],
  )

  const components = useMemo<Components>(() => {
    return {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      code({ inline, className: codeClassName, children, ...props }: any) {
        if (inline) {
          return (
            <code className={classNames('inline-code', codeClassName)} {...props}>
              {children}
            </code>
          )
        }
        return (
          <code className={classNames('code-block', codeClassName)} {...props}>
            {children}
          </code>
        )
      },
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      a({ children, href, ...props }: any) {
        return (
          <a href={href} target="_blank" rel="noreferrer" {...props}>
            {children}
          </a>
        )
      },
      // 自定义引用 chip：react-markdown 默认会忽略未知元素，
      // 这里通过 rehypeRaw 让 <refchip> 进入 AST，再由这个 component 接管渲染。
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      refchip({ node, children, ...props }: any) {
        const rawN = (props as { 'data-n'?: string })['data-n']
        const n = Number(rawN)
        if (!Number.isFinite(n) || n <= 0 || n > maxN) {
          return <span>[{children}]</span>
        }
        const target = refs[n - 1]
        if (!target) {
          return <span>[{n}]</span>
        }
        return (
          <Tooltip
            title={
              <div className="refchip-preview">
                <div className="refchip-preview__title">
                  {buildPreviewTitle(target)}
                </div>
                <div className="refchip-preview__body">
                  {buildPreviewBody(target)}
                </div>
                <div className="refchip-preview__hint">点击查看完整引文</div>
              </div>
            }
            placement="top"
            mouseEnterDelay={0.1}
          >
            <span
              className="refrence-token"
              role="button"
              tabIndex={0}
              onClick={(e) => {
                e.stopPropagation()
                handleChipClick(n)
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  handleChipClick(n)
                }
              }}
            >
              {n}
            </span>
          </Tooltip>
        )
      },
    }
  }, [maxN, refs, handleChipClick])

  return (
    <div
      className={classNames('com-markdown', className)}
      onClick={onClick}
      style={{ lineHeight: '1.6' }}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[
          [rehypeKatex, { strict: false, throwOnError: false }],
          rehypeRaw,
        ]}
        components={components}
        skipHtml={false}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}

export default MarkdownComponent
