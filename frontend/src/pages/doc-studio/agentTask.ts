/**
 * AgentTask 协议（任务派发器 ↔ 任务执行器 之间的统一通信抽象）。
 *
 * 心智模型详见 docs/03-system-mental-model.md §6：
 *   - 报告面板 = 任务派发器；Agent 对话 = 任务执行器（已实装 Cursor 风格 ReAct）
 *   - 报告里所有可点元素都翻译成 AgentTask，由 dispatchTask 完成「联动编辑器 + 切 tab + 注入 composer」
 *   - 与 Agent 后端约定：消息里识别到 <TASK_META>{...}</TASK_META> 时跳过模糊定位
 *
 * 本文件只负责**纯函数 / 工具**：编码、解码、prompt 构造、Monaco 行高亮。
 * 真正的派发动作（接 setPrompt / setRightTab / openFile）在 doc-studio index.tsx 里
 * 用 useCallback 组装，借此避免引入 React Context / 全局 store。
 */

import type * as Monaco from 'monaco-editor'

// ============================================================
// AgentTask 类型
// ============================================================

export type AgentTaskKind = 'evidence_lookup' | 'rewrite_seed' | 'dim_inquiry'

// 阅文五力（docs/08-evaluation-framework.md §3）；compliance 独立合规审核，不进 scorecard
export type DimensionKey =
  | 'story'
  | 'character'
  | 'concept'
  | 'emotion'
  | 'pacing'
export type ComplianceKey = 'compliance'

export interface EvidenceLookupTask {
  kind: 'evidence_lookup'
  evidence_ref_id: string
  scene_id: string
  scene_label?: string | null
  start_line?: number | null
  end_line?: number | null
  /** 可选：报告里展示的 quote，注入 prompt 时让用户能直接看到引用 */
  quote?: string | null
}

/**
 * 段级改写任务的 brief schema。
 *
 * 业内对照（Sudowrite / Notion AI / Cursor Composer / 抖音文心剧本助手）共识：
 * 改写 brief 必须包含【原文 + 维度元数据 + 全剧上下文 + 保留约束 + 改写目标】。
 * 详见 docs/10-rewrite-agent.md §3。
 *
 * 旧版只传 4 字段（dim/scene/issue/evidence_ref_id），等于让 Agent 在零上下文下硬猜，
 * 产出必然是表面润色。本次扩展为可选字段（向后兼容旧 base64 encode），由派发方
 * 尽量填满，未填时 buildPromptFromTask 会降级为兼容旧形态的简化 prompt。
 */
export interface RewriteSeedTask {
  kind: 'rewrite_seed'
  dimension: DimensionKey
  scene_id: string
  scene_label?: string | null
  issue: string
  evidence_ref_id: string

  // 维度元数据：当前分 + LLM 短板理由（来自 scorecard.score / .reason）
  score?: number | null
  dim_reason?: string | null

  // 原文片段：业内全部产品都强调"改写必须吃到原文"——传原文片段 + 集场坐标
  quote?: string | null
  episode_no?: number | null
  scene_no?: string | null

  // 全剧上下文：决定改写的"基调"——题材 / 综合分 / 决策结论
  genre?: string[] | null
  overall_score?: number | null
  decision_label?: string | null
}

export interface DimInquiryTask {
  kind: 'dim_inquiry'
  // 五力 + compliance：用户可以对任何报告卡片询问 Agent
  dimension: DimensionKey | ComplianceKey
  current_score: number | null
}

export type AgentTask = EvidenceLookupTask | RewriteSeedTask | DimInquiryTask

// ============================================================
// URL 编解码（独立 report 页跳回 doc-studio 时用 ?task=base64(...)）
// ============================================================

/**
 * 把 task 编码到 URL safe base64。
 * 不用 JSON.stringify 直接放 query 是因为里面有 quote / 中文，URLEncode 后又长又乱。
 */
export function encodeTaskParam(task: AgentTask): string {
  const json = JSON.stringify(task)
  const b64 = btoa(unescape(encodeURIComponent(json)))
  return b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export function decodeTaskParam(param: string): AgentTask | null {
  if (!param) return null
  try {
    const padded = param.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((param.length + 3) % 4)
    const json = decodeURIComponent(escape(atob(padded)))
    const obj = JSON.parse(json)
    if (!obj || typeof obj !== 'object' || typeof obj.kind !== 'string') return null
    if (!['evidence_lookup', 'rewrite_seed', 'dim_inquiry'].includes(obj.kind)) return null
    return obj as AgentTask
  } catch {
    return null
  }
}

// ============================================================
// 维度中文标签（与 report rail / report 页保持同步）
// ============================================================

const DIM_LABEL: Record<DimensionKey | ComplianceKey, string> = {
  story: '故事力',
  character: '人物力',
  concept: '题材力',
  emotion: '情感力',
  pacing: '叙事力',
  compliance: '合规审核',
}

// ============================================================
// 场次坐标人话化（不要再让用户面对"10-1"这种内部编号）
// ============================================================

/**
 * 把 (episode_no, scene_no, scene_label) 渲染成「第 X 集 · 第 Y 场 · 场景标题」。
 *
 * 输入数据在系统里有三种形态（早期遗留 + LLM 输出 + DB 字段），此函数把它们归一化：
 *   - episode_no 为 1 → "第 1 集"；为 null → 省略集号
 *   - scene_no 是 "10-1" 这种集-场字符串 → 拆出场号；纯数字 → 直接显示
 *   - scene_label "宴会厅 日内" → 作为副标题挂后面
 *
 * 三段都缺时返回空串，让调用方自己兜底（一般传 scene_id.slice(0,6)）。
 */
export function formatSceneLocator(
  episode_no: number | null | undefined,
  scene_no: string | null | undefined,
  scene_label: string | null | undefined,
  options: { withLabel?: boolean } = {},
): string {
  const withLabel = options.withLabel ?? true
  const parts: string[] = []

  // 集号：优先用 episode_no；缺失时从 scene_no="10-1" 形态推断
  let ep = episode_no ?? null
  let sceneNum: string | null = null
  if (typeof scene_no === 'string' && scene_no.trim()) {
    const trimmed = scene_no.trim()
    const dash = trimmed.match(/^(\d+)\s*[-_·\s]\s*(\d+)$/)
    if (dash) {
      if (ep == null) ep = parseInt(dash[1], 10)
      sceneNum = dash[2]
    } else if (/^\d+$/.test(trimmed)) {
      sceneNum = trimmed
    } else {
      sceneNum = trimmed
    }
  }

  if (ep != null && Number.isFinite(ep)) parts.push(`第 ${ep} 集`)
  if (sceneNum) parts.push(`第 ${sceneNum} 场`)

  const head = parts.join(' · ')
  const label = withLabel && scene_label ? scene_label.trim() : ''
  if (head && label) return `${head} · ${label}`
  if (head) return head
  return label || ''
}

/** 紧凑形态："E10·S1"，给 chip / 角标用。 */
export function formatSceneLocatorCompact(
  episode_no: number | null | undefined,
  scene_no: string | null | undefined,
): string {
  let ep = episode_no ?? null
  let sceneNum: string | null = null
  if (typeof scene_no === 'string' && scene_no.trim()) {
    const trimmed = scene_no.trim()
    const dash = trimmed.match(/^(\d+)\s*[-_·\s]\s*(\d+)$/)
    if (dash) {
      if (ep == null) ep = parseInt(dash[1], 10)
      sceneNum = dash[2]
    } else if (/^\d+$/.test(trimmed)) {
      sceneNum = trimmed
    }
  }
  const segs: string[] = []
  if (ep != null && Number.isFinite(ep)) segs.push(`E${ep}`)
  if (sceneNum) segs.push(`S${sceneNum}`)
  return segs.join('·')
}

// ============================================================
// Prompt 构造（task → 人类可读 prompt + <TASK_META> JSON block）
// ============================================================

/**
 * 把 task 翻译成注入 chat composer 的 prompt。
 *
 * 设计原则：
 *   - prompt 是**用户起手模板**：用户能直接发，也能改一两个字再发
 *   - 第一人称、口语化；不要"请简要解释证据→结论的链路"这种学术腔
 *   - 不暴露 scene_id 这种 UUID 噪声（场景定位通过编辑器跳转完成，肉眼可见）
 *   - 底部 <TASK_META> 只给 Agent 读，肉眼可忽略——后端 prompt「任务派发协议」
 *     会要求 Agent 跳过模糊定位、按 kind 直接调对应 tool
 */
export function buildPromptFromTask(task: AgentTask): string {
  const metaBlock = `<TASK_META>${JSON.stringify(task)}</TASK_META>`

  if (task.kind === 'rewrite_seed') {
    return buildRewriteSeedPrompt(task) + '\n\n' + metaBlock
  }

  if (task.kind === 'evidence_lookup') {
    const sceneLabel = task.scene_label || '当前场'
    // quote 已经在编辑器里高亮，不必在 prompt 里复述长引用，会喧宾夺主
    return [
      `我在《${sceneLabel}》看到一条被算法挂为证据的片段（编辑器已高亮）。`,
      `用一两句话告诉我：它对应的是哪个维度？为什么它是这个维度的支撑？`,
      '',
      metaBlock,
    ].join('\n')
  }

  // dim_inquiry
  const dim = DIM_LABEL[task.dimension] || task.dimension
  const scoreHint =
    task.current_score === null || task.current_score === undefined
      ? '尚未给分'
      : `当前 ${task.current_score}/10`
  return [
    `针对「${dim}」维度（${scoreHint}）：`,
    `1) 扣分点最集中的是哪几场？`,
    `2) 这些场各自的核心问题是什么？`,
    `3) 如果只能改 1 场，你建议先改哪一场，怎么改？`,
    '',
    metaBlock,
  ].join('\n')
}

// ============================================================
// 段级改写 prompt 构造
// ============================================================
//
// 输出结构（业内对照：Sudowrite Rewrite mode / Cursor Edit / Notion AI / 抖音文心剧本助手）：
//
//   1. 任务声明（一句话）
//   2. 全剧元数据（题材 + 综合分 + 决策）—— Agent 把握基调
//   3. 维度短板（当前维度 + 评分 + reason）—— 让 Agent 知道改的"病"在哪
//   4. 待改场（坐标 + 原文 quote）—— Agent 不需要再调 tool 拿原文
//   5. 改写目标（按维度差异化）—— 题材力低改人设题材；故事力低改三幕节拍；
//                                 情感力低改钩子密度；叙事力低改起范 + 回报方差；
//                                 人物力低改动机弧光 + 关键关系冲突
//   6. 保留约束（人物关系 / 情节走向 / 题材 / 集数）
//   7. 输出格式（原文 + 改写 + 简短理由 + 预期目标分）
//
// 字段缺失时静默降级（不 throw），旧 RewriteSeedTask 也能跑通。

const REWRITE_GOAL_BY_DIM: Record<DimensionKey, string> = {
  story:
    '修复三幕节拍齐齐全：起范点 / 反转点 / 高潮点必须可见、可感、节奏在合理 beat 上，避免悬念塌陷或反转密度不足。',
  character:
    '强化主角动机弧光与关键关系冲突：动机要可观察（行为 ≠ 设定），冲突要在场景里发生（不是旁白）；OOC 与扁平化必须打掉。',
  concept:
    '抓住题材辨识度与卖点钩子：豪门 / 重生 / 古穿今 / 复仇 等核心标签要落到具象场景，避免类型套路重叠或核心卖点缺锚点。',
  emotion:
    '提高钩子密度与爽点节奏：维持 ≥1 个钩子 / 集，最长无爽点段 ≤ 2 集；情感钩子要有具体触发物（道具 / 台词 / 关键动作）。',
  pacing:
    '校正叙事节奏：开场 ≤ 3 场建立悬念，回报方差控制在合理区间，避免节奏疲软或冲突堆积失衡。',
}

function buildRewriteSeedPrompt(task: RewriteSeedTask): string {
  const dim = DIM_LABEL[task.dimension] || task.dimension
  const locator =
    formatSceneLocator(task.episode_no ?? null, task.scene_no ?? null, task.scene_label ?? null) ||
    task.scene_label ||
    '当前场'

  const lines: string[] = [`改写《${locator}》这场，提升「${dim}」维度。`]

  const ctxParts: string[] = []
  if (task.genre && task.genre.length > 0) ctxParts.push(`题材：${task.genre.join(' / ')}`)
  if (task.overall_score != null) ctxParts.push(`综合分：${task.overall_score.toFixed(1)}/10`)
  if (task.decision_label) ctxParts.push(`决策：${task.decision_label}`)
  if (ctxParts.length > 0) {
    lines.push('', '【全剧基调】', ctxParts.join(' · '))
  }

  const dimParts: string[] = [`当前 ${dim}`]
  if (task.score != null) dimParts[0] += ` ${task.score}/10`
  lines.push('', '【维度短板】', dimParts[0])
  if (task.dim_reason) lines.push(task.dim_reason)
  lines.push(`扣分点：${task.issue}`)

  if (task.quote && task.quote.trim()) {
    lines.push('', '【待改原文】', '```', task.quote.trim(), '```')
  } else {
    lines.push('', '【待改原文】', `（编辑器已定位到《${locator}》，请就近读取上下文）`)
  }

  const goal = REWRITE_GOAL_BY_DIM[task.dimension] || `修复「${dim}」维度的具体扣分点。`
  lines.push('', '【改写目标】', goal)

  lines.push(
    '',
    '【保留约束】',
    '- 人物关系与主线情节走向不变',
    '- 题材标签与集数序列不变',
    '- 仅针对本场，不引入需要修改其他场的连锁改动',
  )

  lines.push(
    '',
    '【输出格式】',
    '1. 原文片段（引用，不要节略）',
    '2. 改写版本（直接可贴回剧本）',
    '3. 改了什么、为什么这样改（≤3 句）',
    `4. 预期 ${dim} 目标分（${task.score != null ? `从 ${task.score}` : '当前未给分'} → 目标 X / 10）`,
  )

  return lines.join('\n')
}

// ============================================================
// Monaco 行级高亮
// ============================================================
//
// 两种使用语义：
//   1. 派发 Agent 类（rewrite_seed / dim_inquiry）—— 给一个短 TTL，让用户视线引导到原文
//      然后视线自然回到 chat composer；3 秒后高亮淡出，避免污染编辑器。
//   2. 溯源（evidence_lookup / 关键场景 / 看点列表）—— **持久高亮**，直到用户点别处溯源
//      或显式取消。这是 task.md §三-2 "保留原文依据"的核心：用户要"对齐论点和论据"，
//      高亮一闪而过等于没高亮。
//
// 实现：highlightLineRange 接受 ttlMs：
//   - ttlMs > 0 → 定时器到点清除（Agent 类用 3000）
//   - ttlMs ≤ 0 / Infinity → 不设定时器，由调用方拿返回的 dispose() 显式清除（溯源类用 0）

const HIGHLIGHT_CLASSNAME = 'scriptlens-evidence-highlight'
const HIGHLIGHT_TTL_MS = 3000

type AnyEditor = {
  getModel?: () => any
  deltaDecorations?: (oldIds: string[], newDecorations: any[]) => string[]
  revealRangeInCenter?: (range: any, scrollType?: number) => void
  revealLineInCenter?: (line: number, scrollType?: number) => void
  focus?: () => void
}

/**
 * 在 Monaco editor 上对 [startLine, endLine] 这段做半透明高亮。
 *
 * 失败模式（容错降级）：
 *   - editor 还没挂载 → 静默返回
 *   - startLine 不合法 → 静默返回
 *   - 行号超出 model 范围 → 钳制到合法范围
 *   - decoration API 不存在 → 静默退化为只滚动定位
 *
 * @param options.ttlMs 高亮自动清除的毫秒数；
 *                      传 `0` / 负数 / `Infinity` 表示**持久高亮**（不会自动清，须调用返回的 dispose）。
 *                      不传则默认 3000ms（派发 Agent 类用）。
 */
export function highlightLineRange(
  editor: AnyEditor | null | undefined,
  startLine: number | null | undefined,
  endLine: number | null | undefined,
  options: { ttlMs?: number; focus?: boolean } = {},
): () => void {
  const noop = () => {}
  if (!editor) return noop
  const model = editor.getModel?.()
  if (!model) return noop
  const lineCount: number = Number(model.getLineCount?.() ?? 0)
  if (!Number.isFinite(lineCount) || lineCount <= 0) return noop

  const monaco = (typeof window !== 'undefined' ? (window as any).monaco : null) as
    | typeof Monaco
    | null
  if (!monaco) return noop

  const safeStart = Math.max(1, Math.min(Math.floor(Number(startLine) || 1), lineCount))
  const rawEnd = Number.isFinite(Number(endLine)) ? Math.floor(Number(endLine)) : safeStart
  const safeEnd = Math.max(safeStart, Math.min(rawEnd, lineCount))
  const range = new monaco.Range(safeStart, 1, safeEnd, 1)

  if (typeof editor.revealRangeInCenter === 'function') {
    editor.revealRangeInCenter(range)
  } else if (typeof editor.revealLineInCenter === 'function') {
    editor.revealLineInCenter(safeStart)
  }
  if (options.focus) editor.focus?.()

  if (typeof editor.deltaDecorations !== 'function') return noop

  const ids = editor.deltaDecorations([], [
    {
      range,
      options: {
        isWholeLine: true,
        className: HIGHLIGHT_CLASSNAME,
        linesDecorationsClassName: `${HIGHLIGHT_CLASSNAME}__gutter`,
      },
    },
  ])

  const ttl = options.ttlMs ?? HIGHLIGHT_TTL_MS
  const isPersistent = !Number.isFinite(ttl) || ttl <= 0
  const timer = isPersistent
    ? null
    : window.setTimeout(() => {
        try {
          editor.deltaDecorations?.(ids, [])
        } catch {
          // editor 已卸载等情况：静默
        }
      }, ttl)

  return () => {
    if (timer != null) window.clearTimeout(timer)
    try {
      editor.deltaDecorations?.(ids, [])
    } catch {
      // 同上
    }
  }
}

export const SCRIPTLENS_EVIDENCE_HIGHLIGHT_CLASS = HIGHLIGHT_CLASSNAME
