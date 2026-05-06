/**
 * Agent 审阅 Diff 组件 - Cursor 风格：全文展示，hunk 处行内高亮
 * 与 Cursor 一致：展示完整文件内容，变更块以 diff 高亮嵌入，Keep 后内容保留为普通行
 * 支持行内编辑：传入 onModifiedContentChange 时，绿色（insert）内容可编辑
 */
import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef } from 'react'
import { structuredPatch } from 'diff'
import { Decoration, Diff, Hunk as HunkView } from 'react-diff-view'
import type { ChangeData, HunkData } from 'react-diff-view'
import 'react-diff-view/style/index.css'

export interface HunkChange {
  originalStartLineNumber: number
  originalEndLineNumber: number
  modifiedStartLineNumber: number
  modifiedEndLineNumber: number
}

type ParsedHunk = HunkData

export interface AgentDiffReviewProps {
  filePath: string
  originalContent: string
  modifiedContent: string
  /** 只读模式（如 Timeline 版本对比），不显示 Undo/Keep 按钮 */
  readOnly?: boolean
  /** 行内编辑：绿色内容可编辑，编辑后通过此回调更新 */
  onModifiedContentChange?: (newContent: string) => void
  diffReverting?: boolean
  currentHunkIndex?: number
  onHunkUndo?: (hunkIndex: number) => void
  onHunkKeep?: (hunkIndex: number, totalHunks: number) => void
  onLineChangesReady?: (changes: HunkChange[]) => void
}

export interface AgentDiffReviewRef {
  /** 从 DOM 读取当前编辑后的 modified 内容（含未 blur 的编辑） */
  getCurrentModifiedContent: () => string
}

function structuredToViewHunk(
  raw: { oldStart: number; oldLines: number; newStart: number; newLines: number; lines: string[] },
): ParsedHunk {
  const { oldStart, oldLines, newStart, newLines, lines } = raw
  const content = `@@ -${oldStart},${oldLines} +${newStart},${newLines} @@`
  let oldLine = oldStart
  let newLine = newStart
  const changes: ChangeData[] = []
  for (const line of lines) {
    const first = line.charAt(0)
    const text = line.slice(1)
    if (first === ' ') {
      changes.push({
        type: 'normal',
        content: text,
        isNormal: true,
        oldLineNumber: oldLine,
        newLineNumber: newLine,
      })
      oldLine++
      newLine++
    } else if (first === '-') {
      changes.push({
        type: 'delete',
        content: text,
        isDelete: true,
        lineNumber: oldLine,
      })
      oldLine++
    } else if (first === '+') {
      changes.push({
        type: 'insert',
        content: text,
        isInsert: true,
        lineNumber: newLine,
      })
      newLine++
    }
  }
  return { content, oldStart, oldLines, newStart, newLines, changes }
}

export const AgentDiffReview = forwardRef<AgentDiffReviewRef, AgentDiffReviewProps>(
  function AgentDiffReview(
    {
      filePath,
      originalContent,
      modifiedContent,
      readOnly = false,
      onModifiedContentChange,
      diffReverting = false,
      currentHunkIndex = 0,
      onHunkUndo,
      onHunkKeep,
      onLineChangesReady,
    },
    ref,
  ) {
  const { hunks, lineChanges, modifiedLines } = useMemo(() => {
    const empty = {
      hunks: [] as ParsedHunk[],
      lineChanges: [] as HunkChange[],
      modifiedLines: [] as string[],
    }
    try {
      const result = structuredPatch(
        filePath || 'file',
        filePath || 'file',
        originalContent ?? '',
        modifiedContent ?? '',
        'original',
        'modified',
        { context: 0 },
      )
      const rawHunks = result?.hunks
      if (!Array.isArray(rawHunks) || !rawHunks.length) {
        return empty
      }
      const hunks = rawHunks.map(structuredToViewHunk)
      const changes: HunkChange[] = hunks.map((h) => ({
        originalStartLineNumber: h.oldStart,
        originalEndLineNumber: h.oldStart + h.oldLines - 1,
        modifiedStartLineNumber: h.newStart,
        modifiedEndLineNumber: h.newStart + h.newLines - 1,
      }))
      const lines = (modifiedContent ?? '').split('\n')
      return { hunks, lineChanges: changes, modifiedLines: lines }
    } catch {
      return empty
    }
  }, [filePath, originalContent, modifiedContent])

  useEffect(() => {
    if (!readOnly) {
      onLineChangesReady?.(lineChanges)
    }
  }, [lineChanges, onLineChangesReady, readOnly])

  const totalHunks = hunks.length
  const hunkBlockRefs = useRef<Map<number, HTMLDivElement | null>>(new Map())
  const containerRef = useRef<HTMLDivElement | null>(null)
  const modifiedLinesRef = useRef<string[]>(modifiedLines)
  modifiedLinesRef.current = modifiedLines

  // 按 hunks 顺序收集所有 insert 行的行号，用于与 DOM 单元格一一对应
  const insertLineNumbers = useMemo(() => {
    const nums: number[] = []
    for (const h of hunks) {
      for (const c of h.changes) {
        if (c.type === 'insert' && c.lineNumber) {
          nums.push(c.lineNumber ?? 0)
        }
      }
    }
    return nums
  }, [hunks])

  useImperativeHandle(
    ref,
    () => ({
      getCurrentModifiedContent: () => {
        const root = containerRef.current
        if (!root) return modifiedLinesRef.current.join('\n')
        const next = [...modifiedLinesRef.current]
        const cells = root.querySelectorAll('.diff-code-insert')
        cells.forEach((cell, i) => {
          const lineNum = insertLineNumbers[i]
          if (lineNum > 0 && lineNum <= next.length) {
            const text = (cell as HTMLElement).innerText.replace(/\n$/, '')
            next[lineNum - 1] = text
          }
        })
        return next.join('\n')
      },
    }),
    [insertLineNumbers],
  )

  // 行内编辑：对绿色 insert 行添加 contentEditable，失焦时同步到 resolvedModified
  useEffect(() => {
    if (!onModifiedContentChange || !containerRef.current) return
    const root = containerRef.current
    const cells = root.querySelectorAll('.diff-code-insert')
    const cleanups: (() => void)[] = []
    cells.forEach((cell) => {
      const el = cell as HTMLElement
      el.contentEditable = 'true'
      const onKeyDown = (e: KeyboardEvent) => {
        if (e.key === 'Enter') e.preventDefault()
      }
      const onBlur = () => {
        const row = el.closest('tr')
        if (!row) return
        const gutter = row.querySelector('.diff-gutter-insert')
        const lineNum = gutter ? parseInt((gutter as HTMLElement).textContent?.trim() || '0', 10) : 0
        if (!lineNum) return
        const newText = el.innerText.replace(/\n$/, '')
        const current = modifiedLines[lineNum - 1] ?? ''
        if (newText !== current) {
          const next = [...modifiedLines]
          next[lineNum - 1] = newText
          onModifiedContentChange(next.join('\n'))
        }
      }
      el.addEventListener('keydown', onKeyDown)
      el.addEventListener('blur', onBlur)
      cleanups.push(() => {
        el.contentEditable = 'false'
        el.removeEventListener('keydown', onKeyDown)
        el.removeEventListener('blur', onBlur)
      })
    })
    return () => cleanups.forEach((fn) => fn())
  }, [onModifiedContentChange, modifiedLines, hunks])

  useEffect(() => {
    if (!readOnly) {
      const el = hunkBlockRefs.current.get(currentHunkIndex)
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
      }
    }
  }, [currentHunkIndex, readOnly])

  if (!hunks.length) {
    return (
      <div className="doc-studio__agent-diff-empty">
        <span>无差异</span>
      </div>
    )
  }

  // 构建全文展示结构：普通行块 + hunk 块 交替
  const segments: Array<
    | { type: 'normal'; lines: string[]; startLine: number }
    | { type: 'hunk'; hunk: ParsedHunk; hunkIdx: number }
  > = []

  for (let i = 0; i < hunks.length; i++) {
    const h = hunks[i]
    const newStart = h.newStart

    // 本 hunk 之前的普通行
    const prevHunkEnd = i === 0 ? 0 : hunks[i - 1].newStart + hunks[i - 1].newLines - 1
    const normalStart = prevHunkEnd + 1
    const normalEnd = newStart - 1
    if (normalEnd >= normalStart) {
      const normalLines = modifiedLines.slice(normalStart - 1, normalEnd)
      segments.push({ type: 'normal', lines: normalLines, startLine: normalStart })
    }

    segments.push({ type: 'hunk', hunk: h, hunkIdx: i })
  }

  // 最后一个 hunk 之后的普通行
  const lastHunk = hunks[hunks.length - 1]
  const lastNewEnd = lastHunk.newStart + lastHunk.newLines - 1
  if (lastNewEnd < modifiedLines.length) {
    const normalLines = modifiedLines.slice(lastNewEnd)
    segments.push({ type: 'normal', lines: normalLines, startLine: lastNewEnd + 1 })
  }

  return (
    <div
      ref={containerRef}
      className="doc-studio__agent-diff-review doc-studio__agent-diff-review--full-file"
    >
      <Diff viewType="unified" diffType="modify" hunks={hunks}>
        {(renderedHunks: ParsedHunk[]) => {
          const hunkMap = new Map(
            (renderedHunks.length ? renderedHunks : hunks).map((h, i) => [i, h]),
          )
          return (
            <>
              {segments.map((seg) => {
                if (seg.type === 'normal') {
                  return (
                    <div
                      key={`normal-${seg.startLine}`}
                      className="doc-studio__agent-diff-normal-block"
                    >
                      {seg.lines.map((line, i) => (
                        <div
                          key={`${seg.startLine + i}`}
                          className="doc-studio__agent-diff-normal-line"
                        >
                          <span className="doc-studio__agent-diff-gutter" />
                          <span className="doc-studio__agent-diff-line-num">
                            {seg.startLine + i}
                          </span>
                          <span className="doc-studio__agent-diff-code">{line || ' '}</span>
                        </div>
                      ))}
                    </div>
                  )
                }
                const { hunk, hunkIdx } = seg
                const viewHunk = hunkMap.get(hunkIdx) ?? hunk
                return (
                  <div
                    key={`hunk-${hunkIdx}`}
                    ref={(el) => {
                      hunkBlockRefs.current.set(hunkIdx, el)
                    }}
                    className="doc-studio__agent-diff-hunk-block"
                  >
                    <Decoration>
                      <div className="doc-studio__agent-diff-hunk-header">{hunk.content}</div>
                    </Decoration>
                    <HunkView hunk={viewHunk} />
                    {!readOnly && (
                      <Decoration>
                        <div
                          className={`doc-studio__agent-diff-hunk-actions${hunkIdx === currentHunkIndex ? ' doc-studio__agent-diff-hunk-actions--active' : ''}`}
                        >
                          <span className="doc-studio__agent-diff-hunk-counter">
                            {hunkIdx + 1} of {totalHunks}
                          </span>
                          <button
                            type="button"
                            className="doc-studio__agent-diff-hunk-btn doc-studio__agent-diff-hunk-btn--undo"
                            disabled={diffReverting}
                            title="Undo (Ctrl/Cmd+N)"
                            onClick={() => onHunkUndo?.(hunkIdx)}
                          >
                            Undo
                          </button>
                          <button
                            type="button"
                            className="doc-studio__agent-diff-hunk-btn doc-studio__agent-diff-hunk-btn--keep"
                            disabled={diffReverting}
                            title="Keep (Ctrl/Cmd+Shift+Y)"
                            onMouseDown={() => (document.activeElement as HTMLElement)?.blur?.()}
                            onClick={() => onHunkKeep?.(hunkIdx, totalHunks)}
                          >
                            Keep
                          </button>
                        </div>
                      </Decoration>
                    )}
                  </div>
                )
              })}
            </>
          )
        }}
      </Diff>
    </div>
  )
  },
)
