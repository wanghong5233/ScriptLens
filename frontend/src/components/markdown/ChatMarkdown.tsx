/**
 * Agent 对话 Markdown 渲染
 * 与主站 Markdown 一致：始终使用 ReactMarkdown + remarkMath + rehypeKatex
 * 通过共享预处理器兼容 \( \) / \[ \] / [ ... ] 等公式格式。
 *
 * ScriptLens 增量：
 *   - 通过 `injectSceneRefLinks` 把"5-3 场 / 第 X 集第 Y 场"转换成 `scriptlens-scene:` 协议链接
 *   - 自定义 `a` renderer：检测协议时拦截点击并 callback 给父组件做场景跳转
 *     这样实现 PRD §5「论点—论据」联动，无需改后端 prompt
 */
import 'katex/dist/katex.min.css'
import './ChatMarkdown.scss'
import { useMemo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import remarkBreaks from 'remark-breaks'
import rehypeKatex from 'rehype-katex'
import rehypeRaw from 'rehype-raw'
import { preprocessMarkdownMath } from './mathPreprocess'
import { injectSceneRefLinks, parseSceneHref } from './sceneRefLink'

type ChatMarkdownProps = {
  children: string
  /**
   * 场景锚点点击回调：href=`scriptlens-scene:5-3` 时被触发，传入 `5-3`。
   * 不传 = 退化为普通文本（不可点击）。
   */
  onSceneRefClick?: (ref: string) => void
}

export function ChatMarkdown({ children, onSceneRefClick }: ChatMarkdownProps) {
  const processed = useMemo(() => {
    const mathPre = preprocessMarkdownMath(children)
    return injectSceneRefLinks(mathPre)
  }, [children])

  const components = useMemo(
    () => ({
      a: ({ children: aChildren, href, ...rest }: any) => {
        const sceneRef = parseSceneHref(href)
        if (sceneRef) {
          const handleClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
            e.preventDefault()
            if (onSceneRefClick) onSceneRefClick(sceneRef)
          }
          return (
            <a
              href={`#scene-${sceneRef}`}
              className="doc-studio-chat-markdown__scene-ref"
              onClick={handleClick}
              data-scene-ref={sceneRef}
              title={`定位到场景 ${sceneRef}`}
            >
              {aChildren}
            </a>
          )
        }
        return (
          <a href={href} target="_blank" rel="noreferrer" {...rest}>
            {aChildren}
          </a>
        )
      },
    }),
    [onSceneRefClick],
  )

  return (
    <div className="doc-studio-chat-markdown doc-studio-chat-markdown--react">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath, remarkBreaks]}
        rehypePlugins={[[rehypeKatex, { strict: false, throwOnError: false }], rehypeRaw]}
        components={components}
      >
        {processed}
      </ReactMarkdown>
    </div>
  )
}
