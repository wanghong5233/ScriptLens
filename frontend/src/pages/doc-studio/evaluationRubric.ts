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
    subtitle: '主线清晰度 + 反转密度',
    definition: '核心主线清晰度 + 情节推进密度 + 反转密度。',
    taskAlignment: '「核心主线是什么」「主要看点 / 钩子 / 反转 / 爽点」',
    signals: [
      '主线 logline 能否 ≤ 60 字讲清',
      '反转事件密度（reversal / face_slap / scheme_exposed / 总集数）',
      '钩子节点完整性（opening / inciting / midpoint / climax / closing 五节拍）',
    ],
  },
  character: {
    label: '人物力',
    subtitle: '主角动机弧光 + 关键关系冲突',
    definition: '主角辨识度 + 动机弧光 + 关键关系冲突。',
    taskAlignment: '「最关键的人物关系和冲突是什么」「角色动机是否成立」',
    signals: [
      '主角动机一句话清晰度（≤ 30 字）',
      '关键决策铺垫充足度（setup_count ≥ 2 比例 / OOC 比例）',
      '强关系数量与极性分布（weight ≥ 0.3 + 主对手关系）',
    ],
  },
  concept: {
    label: '题材力',
    subtitle: '赛道辨识度 + 卖点钩子',
    definition: '赛道辨识度 + 卖点钩子 + 商业可行性。',
    taskAlignment: '「最值得关注的价值是什么」「是否值得继续投入更多时间」',
    signals: [
      '题材标签明确性（重生 / 穿越 / 复仇 / 战神 / 豪门 / 甜宠 / 逆袭）',
      '前 3 场是否出现题材标识事件（死亡 / 绝症 / 重生 / 阴谋揭露 等）',
      '核心卖点能否 ≤ 30 字讲清',
    ],
  },
  emotion: {
    label: '情感力',
    subtitle: '情绪密度 + 爽点频率',
    definition: '情绪密度 + 爽点频率 + 共情触达。',
    taskAlignment: '「主要看点、钩子、反转和爽点在哪里」',
    signals: [
      'reward 事件 / 集数比值',
      '最长连续无 reward 集数（情感塌陷段长度）',
      '首集结尾是否留情绪钩子',
    ],
  },
  pacing: {
    label: '叙事力',
    subtitle: '开场速度 + 节奏方差',
    definition: '开场抓人速度 + 节奏方差 + 信息密度。',
    taskAlignment: '「节奏是否清楚，前半段是否抓人」',
    signals: [
      '首场 20 段内是否出现冲突事件（开场速度信号）',
      '单集事件密度方差',
      '中段平均事件数 / 全剧均值（中段塌陷信号）',
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
    build('high', ['logline 清晰', '反转 / 集 ≥ 2.0', '五个关键节拍完整']),
    build('good', ['logline 基本清晰', '反转 / 集 1.0-2.0', '缺 ≤ 1 个关键节拍']),
    build('medium', ['logline 模糊', '反转 / 集 0.3-1.0', '缺 ≥ 2 个关键节拍']),
    build('low', ['主线讲不清', '反转 / 集 < 0.3', '缺 climax 或 closing 节拍']),
  ],
  character: [
    build('high', [
      '主角 motivation 一句话',
      'setup ≥ 2 占比 ≥ 80% 且 OOC = 0',
      '≥ 3 条强关系（含 ≥ 1 条 negative 主对手）',
    ]),
    build('good', ['主角 motivation 基本清晰', 'setup ≥ 1 占比 ≥ 60% 且 OOC ≤ 2', '≥ 2 条强关系']),
    build('medium', [
      '主角 motivation 模糊或多线分散',
      'setup = 0 占比 ≥ 30% 或 OOC 3-5',
      '≤ 1 条强关系',
    ]),
    build('low', ['主角无可辨识动机', 'OOC > 5 或 ≥ 2 个关键决策无铺垫', '关系图全是弱共现']),
  ],
  concept: [
    build('high', ['落到主流赛道', '首集前 3 场出现题材标识事件', 'core_value 有差异化卖点']),
    build('good', ['落到主流赛道', '首集前 5 场才出现标识事件', 'core_value 清晰但缺差异化']),
    build('medium', ['题材标签泛化（如"都市情感"）', '首集 3 场内无题材标识事件']),
    build('low', ['无可辨识赛道', 'core_value 讲不清']),
  ],
  emotion: [
    build('high', ['reward / 集 ≥ 3.0', '连续无 reward 段 ≤ 1 处', '首集结尾留钩']),
    build('good', ['reward / 集 1.5-3.0', '连续无 reward 段 ≤ 3 处']),
    build('medium', ['reward / 集 0.5-1.5', '存在连续 5+ 集无 reward 段']),
    build('low', ['reward / 集 < 0.5', '中后段连续 8+ 集无 reward']),
  ],
  pacing: [
    build('high', [
      '首场 ≤ 20 段内出现冲突',
      '方差小',
      '中段平均 ≥ 全剧均值 90%',
    ]),
    build('good', [
      '首场 ≤ 30 段内出现冲突',
      '方差中等',
      '中段平均 ≥ 全剧均值 80%',
    ]),
    build('medium', [
      '首场 > 30 段才出现冲突',
      '存在连续 3+ 集低密度段',
      '中段塌陷（< 70%）',
    ]),
    build('low', ['首集前 3 场都在交代背景', '中后段连续 5+ 集低密度']),
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
    signals: ['关键词扫描无命中或全部为虚假命中', '可直接过审'],
  },
  {
    level: 'low_risk',
    tag: '低风险',
    color: 'gold',
    signals: ['命中 ≤ 5 处轻度表达风险（粗口 / 低俗）', '可调整修辞后过审'],
  },
  {
    level: 'medium_risk',
    tag: '中风险',
    color: 'orange',
    signals: ['命中 6-15 处违规倾向（轻度暴力 / 拜金 / 性暗示）', '需复审'],
  },
  {
    level: 'high_risk',
    tag: '高风险',
    color: 'red',
    signals: [
      '命中 ≥ 16 处或触及红线（涉政 / 涉宗教 / 涉历史虚无）',
      '强制 decision 降级为「不建议立项」',
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
