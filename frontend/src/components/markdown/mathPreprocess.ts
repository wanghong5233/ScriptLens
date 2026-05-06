/**
 * Normalize common LLM math delimiters to remark-math friendly form.
 *
 * Why:
 * - Some models emit LaTeX as \(..\) / \[..\] or even [..] blocks.
 * - In certain markdown parse paths, backslash-delimiters can be degraded.
 * - We normalize to $..$ / $$..$$ before rendering.
 *
 * Safety:
 * - Do not rewrite fenced code blocks or inline code.
 */

const LOOKS_LIKE_LATEX =
  /\\(frac|sqrt|sum|int|prod|left|right|alpha|beta|gamma|theta|pi|sigma|lambda|mathcal|mathbf|mathrm|cdot|times|to)|[_^{}]/

function rewriteMathDelimiters(text: string): string {
  if (!text) return text

  let out = text

  // Normalize double-escaped delimiters: \\( -> \(, \\[ -> \[
  out = out.replace(/\\\\([\[\]\(\)])/g, '\\$1')

  // Display: \[...\] -> $$...$$
  out = out.replace(/\\\[([\s\S]*?)\\\]/g, (_, formula: string) => {
    const trimmed = String(formula).trim()
    return trimmed ? `$$${trimmed}$$` : '$$'
  })

  // Inline: \(...\) -> $...$ ; multi-line upgraded to display
  out = out.replace(/\\\(([\s\S]*?)\\\)/g, (_, formula: string) => {
    const trimmed = String(formula).trim()
    if (!trimmed) return '$$'
    if (/\r?\n/.test(trimmed)) return `$$${trimmed}$$`
    return `$${trimmed}$`
  })

  // Non-standard multiline [ ... ] math block -> $$ ... $$
  out = out.replace(/^\s*\[\s*\r?\n([\s\S]*?)\r?\n\s*\]\s*$/gm, (raw: string, formula: string) => {
    const trimmed = String(formula).trim()
    if (!trimmed || !LOOKS_LIKE_LATEX.test(trimmed)) return raw
    return `$$${trimmed}$$`
  })

  // Non-standard single-line [ ... ] math block -> $$ ... $$
  out = out.replace(/^\s*\[(.+?)\]\s*$/gm, (raw: string, formula: string) => {
    const trimmed = String(formula).trim()
    if (!trimmed || !LOOKS_LIKE_LATEX.test(trimmed)) return raw
    return `$$${trimmed}$$`
  })

  return out
}

export function preprocessMarkdownMath(input: string): string {
  if (!input || typeof input !== 'string') return input

  // Protect fenced code blocks first.
  const fenced: string[] = []
  let out = input.replace(/```[\s\S]*?```/g, (segment) => {
    const idx = fenced.push(segment) - 1
    return `@@SM_FENCE_${idx}@@`
  })

  // Protect inline code next.
  const inlineCode: string[] = []
  out = out.replace(/`[^`\n]+`/g, (segment) => {
    const idx = inlineCode.push(segment) - 1
    return `@@SM_INLINE_${idx}@@`
  })

  out = rewriteMathDelimiters(out)

  out = out.replace(/@@SM_INLINE_(\d+)@@/g, (_, n: string) => inlineCode[Number(n)] ?? '')
  out = out.replace(/@@SM_FENCE_(\d+)@@/g, (_, n: string) => fenced[Number(n)] ?? '')

  return out
}

