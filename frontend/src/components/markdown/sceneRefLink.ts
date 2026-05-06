/**
 * 把 chat 文本里的"场景引用"（5-3 场 / 第 5 集第 3 场 / 1-1 等）转成可点击 markdown 链接。
 *
 * 协议：href 用 `scriptlens-scene:{ref}`，ChatMarkdown 的 `a` 自定义渲染检测前缀
 * 并截获 click → 触发场景跳转。
 *
 * 跳过区域：fenced code block (```...```) / 行内 code (`...`) / 已存在的 markdown
 * 链接 ([text](url))。避免把代码或 URL 里的数字组合误转成锚点。
 */

export const SCENE_HREF_PREFIX = 'scriptlens-scene:'

/** 内部模式（带优先级）：先匹配复杂的"第 X 集第 Y 场"，再匹配简化的 "X-Y 场"。 */
type Pattern = {
  re: RegExp
  toRef: (m: RegExpMatchArray) => string
  toLabel: (m: RegExpMatchArray) => string
}

const PATTERNS: Pattern[] = [
  // 第 5 集 第 3 场 / 第5集第3场
  {
    re: /第\s*(\d+)\s*集\s*第\s*(\d+)\s*场/g,
    toRef: (m) => `${m[1]}-${m[2]}`,
    toLabel: (m) => `第${m[1]}集第${m[2]}场`,
  },
  // 5-3 场 / 5-3场（前后不能跟字母数字 / 中划线，避免命中 "tx-1-2-3" 这种 id）
  {
    re: /(?<![A-Za-z0-9_-])(\d+)-(\d+)\s*场/g,
    toRef: (m) => `${m[1]}-${m[2]}`,
    toLabel: (m) => `${m[1]}-${m[2]} 场`,
  },
]

export function injectSceneRefLinks(text: string): string {
  if (!text) return text

  // 1) 先按 fenced code block 分段，code 段保留原样
  const fenceRe = /```[\s\S]*?```/g
  let out = ''
  let lastEnd = 0
  let match: RegExpExecArray | null
  while ((match = fenceRe.exec(text)) !== null) {
    out += processNonFenced(text.slice(lastEnd, match.index))
    out += match[0]
    lastEnd = fenceRe.lastIndex
  }
  out += processNonFenced(text.slice(lastEnd))
  return out
}

function processNonFenced(text: string): string {
  // 2) 在非 fenced 段里跳过：行内 `code` / 已是 [text](url) 的链接
  // 用一个组合 RegExp 找出"保留区"，对其余文本做替换。
  const skipRe = /`[^`\n]+`|\[[^\]\n]*\]\([^)\n]+\)/g
  let out = ''
  let lastEnd = 0
  let match: RegExpExecArray | null
  while ((match = skipRe.exec(text)) !== null) {
    out += applyPatterns(text.slice(lastEnd, match.index))
    out += match[0]
    lastEnd = skipRe.lastIndex
  }
  out += applyPatterns(text.slice(lastEnd))
  return out
}

function applyPatterns(text: string): string {
  let out = text
  for (const { re, toRef, toLabel } of PATTERNS) {
    out = out.replace(re, (...args) => {
      // String.replace callback: (match, p1, p2, ..., offset, string)
      // 重新构造一个 array-like 给 toRef/toLabel 复用 RegExpMatchArray 协议
      const groups = args.slice(0, -2) as unknown as RegExpMatchArray
      const ref = toRef(groups)
      const label = toLabel(groups)
      return `[${label}](${SCENE_HREF_PREFIX}${ref})`
    })
  }
  return out
}

/** 给 ChatMarkdown 的 `a` 自定义 renderer 用：判断 href 是否场景引用。 */
export function parseSceneHref(href: string | undefined): string | null {
  if (!href || !href.startsWith(SCENE_HREF_PREFIX)) return null
  const ref = href.slice(SCENE_HREF_PREFIX.length)
  return ref || null
}
