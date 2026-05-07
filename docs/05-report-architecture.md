# ScriptLens 诊断报告架构

> 本文是诊断报告的**结构契约**。最高准则是 [`source/task.md`](source/task.md)；当 [`01-requirements.md`](01-requirements.md) 与本文冲突时，**新结构以本文为准**，PRD 同步修订。
> 三角分工：03 = 人机协作形态；04 = 数据/代码走向；本文 = 报告内部结构。
> 存储层选型见 [`06-storage-architecture.md`](06-storage-architecture.md)。

## 1. 现状

诊断报告由 4 个独立 chain 并行产出（`asyncio.gather`），前端按「速览 / 故事 / 人物 / 评估」4 segment 组织同一份 `ReportPayload`。task.md §一 列出的 7 类需求一一映射到 segment。

```
                 script_report_service.generate_report
                                │
        ┌────────────┬──────────┴──────────┬───────────────┐
        ▼            ▼                     ▼               ▼
  coverage_chain  beat_chain      character_graph_chain  evaluation_chain
  ─────────────  ──────────      ──────────────────────  ────────────────
  logline        三幕骨架        共现 + LLM 关系分类      5 维等级评估
  recommendation 关键节拍        nodes (动机/目标/阻碍)   evidence_refs
  优 3 / 劣 3    锚点场          edges (类型/极性/权重)   risk_flags
  核心价值       情感弧           appearance_count        rewrite_seeds
        │            │                     │               │
        └────────────┴──────────┬──────────┴───────────────┘
                                ▼
                        ReportPayload (v3)
                                │
                                ▼
              前端 ReportRail 4 segment 重组
              ┌───────┬───────┬───────┬───────┐
              │ 速览  │ 故事  │ 人物  │ 评估  │
              │ 30 s  │ 5 min │ 5 min │ 深度  │
              └───────┴───────┴───────┴───────┘
```

## 2. task.md 需求 → segment 映射

| task.md §一 用户问题 | 映射到 segment | 数据来源 |
|---|---|---|
| 是否值得继续看 | 速览 | `coverage_card.recommendation` |
| 核心价值 | 速览 | `coverage_card.logline` + `core_value` |
| 核心主线 | 故事 | `beat_sheet.acts` |
| 节奏 / 前半段是否抓人 | 故事 | `pacing_curve` + `beat_sheet.acts[0]` |
| 主要看点 / 钩子 / 反转 / 爽点 | 故事 | `beat_sheet.beats` (type=opening/twist/reward) |
| 关键人物关系和冲突 | 人物 | `character_graph` |
| 角色动机是否成立 | 人物 | `character_graph.nodes[].motivation` + `evaluation.dimensions[motivation]` |
| 问题和风险 | 评估 | `risk_flags` |
| 数据等级评估 (§五 3) | 评估 | `evaluation.dimensions[]` (5 维) |
| 改写建议 (§五 4) | 评估 | `rewrite_seeds[]` |

**任何结论必须能回到原文锚点（task.md §三 2「保留原文依据」）**：所有可点元素携带 `evidence_ref_id` 或 `scene_id`。

## 3. 分层职责

| 层 | 负责 | 不负责 |
|---|---|---|
| `coverage_chain` | logline ≤ 60 字 / recommendation 三档 / 类型 1-3 / 3 优 + 3 劣 / 核心价值 ≤ 30 字 | 评分、节拍、关系 |
| `beat_chain` | 三幕骨架（开局/发展/收束）/ 关键节拍（开场/激励/中点/高潮/收束/反转/爽点）/ 节拍锚点 scene_id | logline、关系、评分 |
| `character_graph_chain` | 共现统计 / 关系类型分类 / 节点属性（动机/目标/阻碍） / 关系极性 | 节奏、节拍、看点 |
| `evaluation_chain` | 5 维等级评分 + reason + evidence_ref_ids / 风险清单 / 改写候选 | 故事抽象、人物关系 |
| `pacing_aggregator`（无 LLM） | 每集事件密度 / hooks / twists / reward_events / 情感弧分值 | 评分、节拍 |
| `script_view_service`（视图层） | 派生 `task_status` / `role` 视角重排 / 不重生成 | 改 ReportPayload、预生成 rewrite 文本 |

## 4. 第一性原理

| 维度 | 分析 | 结论 |
|---|---|---|
| 对照 task.md §三 1 痛点 | 长 + 信息分散 + 判断质量取决于关键信息提取是否对 | 报告必须分层 + 任意结论可溯源；摘要不够 |
| 对照 task.md §三 2 输出 | 「分层呈现满足不同时间预算」+「保留原文依据」+「快速定位关键部分」 | 30s/5min/深度三层 + 全量 evidence_ref + 点击跳原文 |
| 对照 task.md §三 4 核心功能 | 结构化分析 + 追问 + 看依据 + 切视角 + 迭代修正 | 报告作为 dispatcher 接 03 的 AgentTask 协议；不在报告里实装 chat |
| 对照 task.md §六 期待 | 「围绕用户决策设计结果，不是围绕文本做机械摘要」 | 速览作为决策入口（logline + recommendation），不让用户先翻 5 维分数再决策 |
| 决策路径长度 | 5 维评分先看分→读 reason→翻证据，路径长且要求用户理解维度含义 | coverage_card 一屏给 logline + 推荐 + 优劣，30 秒可决策；5 维评分降级为深度层 |
| 故事抽象工业惯例 | Hollywood Studio Coverage 60 年标准（logline + recommend + 优劣）；Field 1979 三幕；Snyder 2005 Save the Cat 15 节拍；Reagan et al. 2016《Six basic shapes of stories》情感弧 | logline + recommendation 借鉴 Coverage；三幕骨架借鉴 Field；情感弧借鉴 Reagan |
| 短剧节奏特殊性 | 短剧每集需钩子，每 2-3 集需反转，集尾留钩；与 90 分钟商业片节奏不同 | 整剧用三幕（开局/发展/收束），单集事件密度 + 情感弧走 pacing_curve；不套 Save the Cat 15 节拍 |
| 人物理解形式 | 学术（Elson 2010 共现网络）+ 工业（豆瓣 / Wikipedia 关系图）共识用图 | 共现矩阵给候选边 + LLM 给关系类型 + 力导向布局；避免「只共现没关系类型」与「纯 LLM 不稳」 |
| LLM 调用成本 | 单 chain 1-2 次调用，4 chain 串行 ≈ 1-2 分钟 | 4 chain 并行（asyncio.gather），总耗时 ≈ 最慢的一个 |
| 关键场景挑选 | 旧链路用 5 维评分证据当关键场，导致动作行被当关键场（「电视上放着猫和老鼠」） | beat_chain 给的节拍锚点场 = 关键场；scene_summary 由 LLM 全场总结，不是 quote 截取 |
| 可视化库选型 | react-force-graph-2d ~150KB / vis-network ~500KB / cytoscape ~700KB / 自写 SVG 工作量大 | react-force-graph-2d（D3 内核 + API 简单 + 体积可控） |
| 5 维评分定位 | task.md §五 3 是加分项，要的是「等级判断或量化分析」，不指定形式 | PRD §6 的 5 维（opening_hook / reward_density / motivation / pacing / risk）保留，作为评估 segment 的「数据评估卡」 |

## 5. 数据契约

```ts
ReportPayload (v3) {
  // 既有（PRD §7 基线，逐步迁移）
  decision        : { label, confidence, one_sentence_reason, must_read_scene_ids }
  scorecard       : DimScore[]   // 旧字段，过渡期内与 evaluation.dimensions 同源；后续删
  evidence_refs   : EvidenceRef[]
  characters      : Character[]  // 旧扁平列表，由 character_graph 取代

  // 新增（v3 segment 对应）
  coverage_card: {
    logline:        string                          // ≤ 60 字一句话
    recommendation: 'recommend' | 'consider' | 'pass'
    confidence:     'high' | 'medium' | 'low'
    genre:          string[]                        // 1-3 个
    core_value:     string                          // ≤ 30 字「最值得关注的价值」
    strengths:      { title, detail, anchor_scene_id? }[]   // 3 项
    concerns:       { title, detail, anchor_scene_id? }[]   // 3 项
  }

  beat_sheet: {
    acts: {
      act:         1 | 2 | 3
      title:       string                           // 开局 / 发展 / 收束
      scene_range: [scene_id, scene_id]
      beats: {
        type:            'opening' | 'inciting' | 'midpoint' | 'climax'
                       | 'closing' | 'twist' | 'reward'
        summary:         string                     // ≤ 50 字
        anchor_scene_id: string
      }[]
    }[]
  }

  character_graph: {
    nodes: {
      id:               string                      // 角色稳定 slug
      name:             string
      role:             'protagonist' | 'antagonist' | 'support' | 'minor'
      motivation:       string                      // ≤ 30 字
      goal:             string
      obstacle:         string
      first_scene_id:   string
      appearance_count: number
    }[]
    edges: {
      source_id: string
      target_id: string
      type:      'family' | 'romance' | 'rival' | 'ally'
               | 'authority' | 'deception' | 'mentor'
      weight:    number                             // 0-1，共现强度归一
      polarity:  'positive' | 'negative' | 'mixed'
    }[]
  }

  pacing_curve: {
    episode_no:    number
    scene_count:   number
    event_count:   number
    hooks:         number
    twists:        number
    reward_events: number
    sentiment:     number                           // -1.0~+1.0 情感弧分值
  }[]

  evaluation: {
    dimensions: {
      key:               'opening_hook' | 'reward_density' | 'motivation' | 'pacing' | 'risk'
      label:             string                     // 中文展示名
      score:             number | null              // 0-10；证据不足 null
      level:             'high' | 'medium' | 'low' | 'high_risk' | null
      reason:            string
      evidence_ref_ids:  string[]
    }[]
    risk_flags:    string[]
    rewrite_seeds: { dimension, scene_id, issue, severity, evidence_ref_id }[]
  }
}
```

**不变式**：

- `coverage_card.logline` ≤ 60 字；`core_value` ≤ 30 字；禁出现「秒/镜头/画面/分镜」等成片词
- `beat_sheet.acts` 必须覆盖 1-3 幕；每幕至少 1 节拍；节拍 `anchor_scene_id` 必须存在于 `scenes` 表，校验失败丢弃该节拍
- `character_graph.nodes` ≤ 12 个；超过按 `appearance_count` 截前 12
- `character_graph.edges` 按 `(min(source,target), max(source,target))` 双向去重；`weight < 0.15` 丢弃
- `pacing_curve.length == scripts.episode_count`；缺集补 `event_count = 0` 与 `sentiment = 0`
- `evaluation.dimensions` 长度恒为 5；`score=None` 的维度也保留（PRD §6 「证据不足不伪造默认分」）
- 任一 chain 失败 → 对应顶层字段为 `null`，**不返默认值**（fail aloud，core-principles）

## 6. 前端 4 segment

| segment | 数据 | 心智 | 主要交互 |
|---|---|---|---|
| **速览** | `coverage_card` + `character_graph` 缩略（top 5 by `appearance_count`）+ `pacing_curve` 缩略 | 30 秒决策 | 看 logline / 推荐 / 核心价值 / 优劣 |
| **故事** | `beat_sheet` + `pacing_curve` 完整（事件 + 情感弧） | 5 分钟读懂故事 | 点节拍 → 编辑器跳锚点场 + 持久高亮 |
| **人物** | `character_graph` 完整（force-directed） | 5 分钟读懂人物 | 点节点 → 编辑器跳首场；点边 → 看共现场列表 |
| **评估** | `evaluation` + `evidence_refs` | 验证 + 行动 | 点证据跳原文；点改写候选派 Agent |

`role` 视角切换（选品 / 编剧 / 审核）只在 segment 内部重排，不重生成报告（PRD §7 不变式）。

## 7. 可逆性

| 触发条件 | 触发后动作 |
|---|---|
| `character_graph_chain` schema 校验失败率 ≥ 5 起 | 关系类型降级为「相关 / 对抗」二分类，前端图保留 |
| `beat_chain` LLM 失败 ≥ 3 起 | 退化为按 scene 数三等分给三幕骨架，节拍留空 |
| Agent FTS 召回率 < 60% 持续 5 起 | jieba tokenizer 升级为「jieba + 自定义剧本词典」（人名/职位/成语） |
| 单剧 `character_graph.nodes` > 50 | 拆主线/副线 graph，segment 内多 graph 切换 |
| 4 chain 并行总耗时 > 5 分钟 | `coverage_chain` / `evaluation_chain` 用 ModelTier.MINI |
| 短剧情感弧不稳 | 退化为只给整剧三幕 + 事件密度，去掉 sentiment 字段 |
| 5 维评分长期被用户忽略（usage 埋点） | 替换为 task.md §五 3 提示的「质量 / 可读性 / 结构完整度 / 节奏 / 潜在表现」5 维 |

**触发判断以运行时指标为准，不在无数据时提前决策。**
