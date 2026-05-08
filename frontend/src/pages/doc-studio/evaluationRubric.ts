/**
 * 阅文五力评估 rubric 锚点表（前端常量 · 与 docs/08-evaluation-framework.md §3 同步）。
 *
 * 业内对照：rubric 锚点（评分标准）属于"框架级"定义而非"实例级"产出，业内主流
 * 把它放在前端常量 / 配置文件，不每次让 LLM 现编：
 *   - Sudowrite Manuscript Analysis：rubric 前端常量
 *   - Grammarly Tone / Confidence 评分：i18n 资源
 *   - Coursera Smart Review：课程模板 JSON
 *   - Elsevier / EditPro 学术评审：平台模板
 *   - ESLint / SonarQube：内置规则配置
 * 详细论证见 docs/10-rewrite-agent.md §3 + docs/08-evaluation-framework.md §8（待补可逆性条件）。
 *
 * 切换到后端字段化的触发条件：rubric 进入 A/B 测试 / 多语言 / 行业自定义阶段。
 */

import type { DimensionKey, ComplianceKey } from './agentTask'

// ============================================================
// 维度元数据（中文标签 + 副标题 + 定义 + task.md 对齐 + 信号源）
// ============================================================

export interface DimensionMeta {
  /** 中文标签：故事力 / 人物力 / ... */
  label: string
  /** 副标题：「主线清晰度 + 反转密度」等可观测信号简述 */
  subtitle: string
  /** 维度定义一句话 */
  definition: string
  /** task.md §三-1 的对应条目（让用户看到"为什么这个维度存在"） */
  taskAlignment: string
  /** 后端打分依赖的剧本侧可观测信号 */
  signals: string[]
}

export const DIMENSION_META: Record<DimensionKey, DimensionMeta> = {
  story: {
    label: '故事力',
    subtitle: '主线清不清楚 / 反转够不够密',
    definition: '主线讲不讲得清楚，剧情推进有没有钩子，有没有让人想往下看的反转。',
    taskAlignment: '「核心主线是什么」「主要看点 / 钩子 / 反转 / 爽点」',
    signals: [
      '主线一句话能不能讲清楚',
      '反转出现的频率（每几集会有一次大反转）',
      '三幕节拍是否完整：开场 / 推进 / 高潮 / 收束',
    ],
  },
  character: {
    label: '人物力',
    subtitle: '主角立不立得住 / 关系够不够撑冲突',
    definition: '主角让不让人记住，动机能不能解释他的行为，人物关系够不够撑起冲突。',
    taskAlignment: '「最关键的人物关系和冲突是什么」「角色动机是否成立」',
    signals: [
      '主角想要什么、能不能一句话说清',
      '关键决策有没有铺垫（不会让人觉得"这角色为啥这么干"）',
      '人物关系够不够，是不是有明确的主反派 / 主对手',
    ],
  },
  concept: {
    label: '题材力',
    subtitle: '赛道辨识度 + 卖点钩子',
    definition: '看得出这是哪个赛道（战神 / 重生 / 复仇 / 甜宠等），有没有让人想继续看的差异化卖点。',
    taskAlignment: '「最值得关注的价值是什么」「是否值得继续投入更多时间」',
    signals: [
      '题材标签是否落到主流爆款赛道',
      '首集前 3 场就点出题材标识事件（重生 / 退婚 / 复仇 / 当众羞辱 等）',
      '核心卖点能不能一句话讲清，并且和别的剧不一样',
    ],
  },
  emotion: {
    label: '情感力',
    subtitle: '看得过不过瘾 / 爽点够不够密',
    definition: '看得过不过瘾，每集是不是都有让人爽 / 哭 / 燃的情绪钩子，整剧有没有"好几集没爽点"的塌陷段。',
    taskAlignment: '「主要看点、钩子、反转和爽点在哪里」',
    signals: [
      '平均每集出现几个情绪钩子',
      '有没有连续好几集都没爽点（情感塌陷段）',
      '首集结尾是否留下让人想追下去的情绪钩',
    ],
  },
  pacing: {
    label: '叙事力',
    subtitle: '开场抓不抓人 / 节奏稳不稳',
    definition: '开场抓不抓人，整剧节奏稳不稳，中段会不会"突然没事发生"。',
    taskAlignment: '「节奏是否清楚，前半段是否抓人」',
    signals: [
      '首场就来冲突，还是花很多笔墨交代背景',
      '每集事件密度稳定，不会忽快忽慢',
      '中段（剧的中间 1/3）密度有没有比全剧均值低（中段塌陷）',
    ],
  },
}

export const COMPLIANCE_META: { label: string; subtitle: string; definition: string } = {
  label: '合规审核',
  subtitle: '广电八关 + 6 类红线',
  definition: '广电监管「八关」+ 6 类红线扫描；独立维度，不计入综合评分；high_risk 强制把整剧决策降为「不建议立项」。',
}

// ============================================================
// 4 档 rubric 锚点（每档对应一个 score 区间）
// ============================================================

export type RubricLevel = 'high' | 'good' | 'medium' | 'low'

export interface RubricAnchor {
  level: RubricLevel
  /** 分数区间显示（"9-10" / "6-8" / "3-5" / "0-2"） */
  range: string
  /** 档位标签（"优秀 / 良好 / 中等 / 待改"） */
  tag: string
  /** 档位颜色（与 antd Tag color 对齐） */
  color: 'green' | 'cyan' | 'orange' | 'red'
  /** 触发该档的剧本字面信号（与 docs/08 §3 完全同步） */
  signals: string[]
}

const TAG: Record<RubricLevel, { range: string; tag: string; color: RubricAnchor['color'] }> = {
  high: { range: '9-10', tag: '优秀', color: 'green' },
  good: { range: '6-8', tag: '良好', color: 'cyan' },
  medium: { range: '3-5', tag: '中等', color: 'orange' },
  low: { range: '0-2', tag: '待改', color: 'red' },
}

function build(level: RubricLevel, signals: string[]): RubricAnchor {
  return { level, ...TAG[level], signals }
}

export const DIMENSION_RUBRICS: Record<DimensionKey, RubricAnchor[]> = {
  story: [
    build('high', [
      '主线一句话讲清',
      '反转钩子密集（每两集就有一次大反转）',
      '开场 / 推进 / 高潮 / 收束 节拍齐全',
    ]),
    build('good', [
      '主线基本清晰',
      '每三集有一次反转',
      '只缺 1 个关键节拍',
    ]),
    build('medium', [
      '主线偏模糊',
      '反转稀疏（4-10 集才一次）',
      '缺 ≥ 2 个关键节拍',
    ]),
    build('low', [
      '主线讲不清',
      '几乎没有反转',
      '缺高潮或收束',
    ]),
  ],
  character: [
    build('high', [
      '主角有一句话讲清的目标',
      '关键决策都有铺垫，不会突兀',
      '至少 3 条强人物关系，含明确主反派',
    ]),
    build('good', [
      '主角动机大致清楚',
      '多数关键决策铺垫到位',
      '至少 2 条强人物关系',
    ]),
    build('medium', [
      '主角动机模糊或多线分散',
      '约 30%+ 关键决策没有铺垫',
      '人物关系单薄，主对手不清晰',
    ]),
    build('low', [
      '主角没有可辨识动机',
      '多个关键决策行为突兀',
      '人物关系全是弱共现',
    ]),
  ],
  concept: [
    build('high', [
      '题材落到主流爆款赛道（战神 / 重生 / 复仇 / 甜宠等）',
      '首集前 3 场就出现题材标识事件',
      '卖点能一句话讲清且与别的剧不同',
    ]),
    build('good', [
      '题材在主流赛道',
      '首集前 5 场才点题',
      '卖点清晰但不够独特',
    ]),
    build('medium', [
      '题材标签泛化（如"都市情感"）',
      '首集 3 场内点不出题材',
    ]),
    build('low', [
      '看不出是哪个赛道',
      '卖点讲不清',
    ]),
  ],
  emotion: [
    build('high', [
      '每集都有情感钩子（≥ 1.5 / 集）',
      '几乎不会出现"几集都没爽点"',
      '首集结尾留钩',
    ]),
    build('good', [
      '多数集有钩子（每集 0.8-1.5）',
      '偶尔会有 ≤ 3 集塌陷',
    ]),
    build('medium', [
      '钩子偏少（每集 0.3-0.8）',
      '存在 ≥ 5 集连续没爽点',
    ]),
    build('low', [
      '基本没钩子（每集 < 0.3）',
      '中后段连续 ≥ 8 集没爽点',
    ]),
  ],
  pacing: [
    build('high', [
      '开场 600 字内就来冲突',
      '每集事件密度稳定',
      '中段达全剧均值 90%+',
    ]),
    build('good', [
      '开场较快进入冲突',
      '密度波动适中',
      '中段达全剧均值 80%+',
    ]),
    build('medium', [
      '开场超过 1000 字才发力',
      '存在 ≥ 3 集低密度段',
      '中段塌陷到全剧 70% 以下',
    ]),
    build('low', [
      '首集前 3 场都在交代背景',
      '中后段连续 ≥ 5 集低密度',
    ]),
  ],
}

// ============================================================
// 合规 4 档（与 backend `compliance.level` 枚举同步）
// ============================================================

export interface ComplianceRubricAnchor {
  level: 'clean' | 'low_risk' | 'medium_risk' | 'high_risk'
  tag: string
  color: 'green' | 'gold' | 'orange' | 'red'
  signals: string[]
}

export const COMPLIANCE_RUBRIC: ComplianceRubricAnchor[] = [
  {
    level: 'clean',
    tag: '安全',
    color: 'green',
    signals: ['没有触发广电红线 / 灰线词', '可直接过审'],
  },
  {
    level: 'low_risk',
    tag: '低风险',
    color: 'gold',
    signals: ['局部出现粗口 / 软色情暗示等表达风险', '调整措辞后可过审'],
  },
  {
    level: 'medium_risk',
    tag: '中风险',
    color: 'orange',
    signals: [
      '题材含主流风险元素（拜金 / 暴力链条 / 伦理纠纷 / 医疗失实 / 极端复仇）',
      '需复审，重要桥段建议改写',
    ],
  },
  {
    level: 'high_risk',
    tag: '高风险',
    color: 'red',
    signals: [
      '触及广电红线（未成年涉性 / 制毒贩毒方法 / 自杀方法 / 民族敏感）',
      '不可发布；整剧决策强制降为「不建议立项」',
    ],
  },
]

// ============================================================
// 工具函数
// ============================================================

/**
 * 把 0-10 分映射到 4 档 rubric anchor。
 * 用于：维度卡展开时高亮当前所在档。
 *
 * 边界：null / NaN / 越界 → 'low'（fail aloud 不沉默返回任意默认）
 */
export function getRubricLevel(score: number | null | undefined): RubricLevel {
  if (score == null || !Number.isFinite(score)) return 'low'
  if (score >= 9) return 'high'
  if (score >= 6) return 'good'
  if (score >= 3) return 'medium'
  return 'low'
}

/** 维度 key → 完整 meta（合规走 COMPLIANCE_META 单独入口） */
export function getDimensionMeta(key: DimensionKey | ComplianceKey): DimensionMeta | null {
  if (key === 'compliance') return null
  return DIMENSION_META[key] || null
}
