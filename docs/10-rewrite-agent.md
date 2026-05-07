# ScriptLens 改写 Agent 设计

> 「行动 · 编剧」segment 的执行后端。回答「我现在该改哪段、怎么改、改完什么样」。

## 1. 现状陈述

改写动作族分两档，前端入口收敛在编剧 Persona Action Card 内：

```
编剧卡 (单组件)
├── Hero · 全剧改写计划 (primary 按钮 · Plan-then-Execute · 治本)
└── 段级精修列表 (rewrite_seeds 完整渲染 · 不限 3 条 · 治标)
```

数据流向：

```
ReportPayload (后端持久化)
    │  scorecard / rewrite_seeds / coverage_card.genre / decision / overall_score / evidence_refs
    ▼
ViewResponse (router 透传)
    ▼
WriterActionCard (前端) ── 拼 brief ──┐
    │                                  │
    ├── 段级 dispatch  rewrite_seed  ──┤
    └── 全剧 dispatch  fulltext_rewrite (Step 2)
                                       ▼
                            Agent (ReAct loop · OpenAI / DashScope)
                                       │
                                       ▼
                            AgentDiffReview (单文件多 hunk · Cursor 风格)
```

不再有"评估 § 最值得改写"段——评估 segment 只承载诊断信息。详见 docs/09-action-lens.md §1。

## 2. 分层职责表

| 层 | 负责 | 不负责 |
|---|---|---|
| `ReportPayload` (后端) | rewrite_seeds 候选 (低分段定位 + dimension + issue + evidence_ref_id) | 改写文本生成、改写计划编排 |
| `WriterActionCard` (前端) | 段级 brief 拼装、全剧 plan dispatch、UI 列表渲染 | 改写文本生成、diff 应用 |
| `agentTask.buildRewriteSeedPrompt` | 把 RewriteSeedTask 翻译为带【原文 + 元数据 + 目标 + 约束】的完整 prompt | 任务派发、UI 状态 |
| Agent runtime (ReAct loop) | 执行改写、按需调 tool 拿原文上下文、产出原文 → 改写 → 理由结构化输出 | UI 渲染、diff 编辑 |
| `AgentDiffReview` | 单文件多 hunk Cursor 风格审阅、行内编辑、prev/next 导航、Keep/Undo per hunk | 多文件 diff（不需要） |

## 3. 第一性原理

| 维度 | 分析 | 结论 |
|---|---|---|
| 短剧体量 | 单本 ≈ 100 集 × 5 场 ≈ 25-30K tokens | GPT-5.2 / Qwen-max 长上下文一吃即下，全剧 plan 可行 |
| 痛点性质 | 抖音 / 快手短剧典型问题为结构性（钩子密度 / 反转齐 / 节奏方差） | 段级精修治标，全剧 plan 治本 |
| 五力短板形态 | 故事力 / 情感力 / 叙事力 三维同时低分时，逐段改 ≈ 按图打地鼠 | 必须有全局整改入口 |
| 文件结构 | 一本剧 = 一个文件，集 / 场是文件内 line range | 全剧改写产出落同一文件，复用现有单文件多 hunk diff，不需要 multi-file diff |
| Token 上限 | 一次输出整本剧本 (≈ 30K out tokens) 不可控、不可逆 | 全剧改写必须分两步：plan → execute，每 step 走段级改写产生 hunk |
| 改写 brief 内容 | 旧版只传 4 字段 (dim / scene / issue / evidence_ref_id)，Agent 收到等于零上下文 | brief 必须含原文 + 维度评分 + 全剧元数据 + 改写目标 + 保留约束 |
| 改写动作差异化 | 题材力低改人设 / 故事力低改节拍 / 情感力低改钩子 / 叙事力低改起范 + 回报 | 按 dimension 差异化目标，不能套同一句"保留人物关系和情节走向" |
| 业内同构方案 | Cursor Composer / Copilot Workspace / 抖音文心剧本助手 / 快手 StreamLake 全采 Plan-then-Execute | 直接套 |

## 4. 段级改写 brief 契约

```text
RewriteSeedTask {
  kind: 'rewrite_seed'
  dimension: 'story' | 'character' | 'concept' | 'emotion' | 'pacing'
  scene_id: string
  scene_label?: string
  issue: string
  evidence_ref_id: string

  score?: number              # 维度当前分
  dim_reason?: string         # 维度短板理由（来自 scorecard.reason）
  quote?: string              # 待改原文片段
  episode_no?: number         # 集场坐标
  scene_no?: string
  genre?: string[]            # 全剧元数据
  overall_score?: number
  decision_label?: string
}
```

**派发方填充约束**（详见 `WriterActionCard.dispatchSeed` 实装）：

| 字段 | 来源 | 缺失行为 |
|---|---|---|
| `score` | `view.scorecard[dim].score` | prompt 退化为"未给分" |
| `dim_reason` | `scorecard[dim].reason` 的首句（≤200 字） | 跳过【维度短板】块 |
| `quote` | `evidenceMap.get(evidence_ref_id).quote` | prompt 提示「编辑器已定位，请就近读取」 |
| `episode_no / scene_no` | 同 evidence ref | 由 scene_label 兜底 |
| `genre / overall_score / decision_label` | `view.coverage_card / view.overall_score / view.decision.label` | 跳过【全剧基调】块 |

prompt 输出结构（`buildRewriteSeedPrompt`）：

```text
1. 任务声明（一句话定位）
2. 【全剧基调】题材 · 综合分 · 决策
3. 【维度短板】当前 dim score/10 + dim_reason + 扣分点
4. 【待改原文】quote（缺失时降级为定位提示）
5. 【改写目标】按 dimension 差异化（见下表）
6. 【保留约束】人物关系 / 情节走向 / 题材 / 集数序列不变；不引入跨场连锁改动
7. 【输出格式】原文 + 改写版本 + 改写理由 + 预期目标分
```

按 dimension 差异化的【改写目标】：

| dimension | 目标 |
|---|---|
| `story` | 三幕节拍齐齐全：起范点 / 反转点 / 高潮点必须可见、可感、节奏在合理 beat 上 |
| `character` | 强化主角动机弧光与关键关系冲突；动机要可观察（行为 ≠ 设定）；OOC 与扁平化必须打掉 |
| `concept` | 抓住题材辨识度与卖点钩子；豪门 / 重生 / 古穿今 / 复仇 等核心标签落到具象场景 |
| `emotion` | 维持 ≥1 钩子 / 集，最长无爽点段 ≤ 2 集；情感钩子要有具体触发物（道具 / 台词 / 关键动作） |
| `pacing` | 校正叙事节奏：开场 ≤ 3 场建立悬念，回报方差控制在合理区间 |

## 5. 全剧改写计划 (Plan-then-Execute · Step 2 路线)

**当前状态**：编剧卡 hero「让 Agent 出改写计划」按钮存在，点击触发 `message.info` 提示。后端实装待 Step 2。

```text
FulltextRewriteTask {
  kind: 'fulltext_rewrite'
  weak_dims: { dimension, score, reason }[]      # 五力短板（< 6 的全部）
  decision_label: string
  overall_score: number
  genre: string[]
  rewrite_seeds: RewriteSeedDTO[]                # 已识别的段级候选
  beat_summary?: string                           # 三幕摘要（取 view.beat_sheet 派生）
}
```

执行流程：

```
1. 用户点 hero 按钮
   ↓
2. dispatch fulltext_rewrite，Agent 吃 brief 输出改写计划
   plan = [
     { step_id, scope: 'episode' | 'dimension', target, reason, est_seeds: RewriteSeedDTO[] },
     ...
   ]
   ↓
3. 前端在 chat 里渲染 plan 树（可勾选 / 单选 / 全选）
   ↓
4. 用户审 plan → 确认执行
   ↓
5. 前端逐 step 触发段级 rewrite_seed（复用 §4 brief 契约 + step.target 注入）
   ↓
6. 改写 hunk 累积到同一剧本文件 → AgentDiffReview Cursor 风格审阅
```

**Step 2 必须补的能力**：

| 能力 | 定位 | 工作量估算 |
|---|---|---|
| `FulltextRewriteTask` kind + `buildPromptFromTask` 分支 | 前端 `agentTask.ts` | S |
| Plan 渲染组件（chat 里的可勾选树） | 前端新组件 | M |
| Agent intent rule (`fulltext_rewrite`) | 后端 `agent_runtime/configs/intent_rules.json` + prompt | M |
| Plan → 逐 step 派发循环 | 前端 dispatchTask 扩展 | M |
| **Accept All / Reject All** | 现有 `AgentDiffReview` 加全局按钮 + 快捷键 | S |

## 6. Diff 机制复用

短剧 = 单文件，集 / 场是文件内 line range（`EvidenceRefDTO.episode_no / scene_no / start_line / end_line` 全是单文件坐标）。全剧改写产出的 N 个 hunk 全部落在同一文件，**直接复用 `AgentDiffReview` 单文件多 hunk Cursor 风格**：

| 现有能力 | 用于改写 |
|---|---|
| 单文件多 hunk inline diff | 全剧改写多 step 累积的 hunk 同屏审阅 |
| `currentHunkIndex` prev / next 导航 | 50 hunk 顺序扫 |
| `onHunkKeep / onHunkUndo` per hunk | 单段接受 / 撤回 |
| 行内编辑（绿色 insert 行可改） | 改写后微调 |

**未实装的 multi-file 能力不需要**——剧本不跨文件。

## 7. 接口契约

```text
AgentTask = EvidenceLookupTask | RewriteSeedTask | DimInquiryTask | FulltextRewriteTask (Step 2)
```

**不变式**：

- `RewriteSeedTask` 新增字段全部可选，向后兼容旧 base64 encode（URL 跳转场景）
- `buildRewriteSeedPrompt` 必须接受所有字段缺失的 fallback path
- 改写文本不持久化到 `ReportPayload`——改写产出走 chat session + diff 落盘
- 合规违规不进 `rewrite_seeds`（红线问题人工复审，LLM 不下场改）

## 8. 可逆性

| 决策 | 切换触发条件 |
|---|---|
| 段级 brief 由前端拼 | 当 brief 字段超过 10 个 / 维度差异化目标超过 3 套 → 移到后端 view 派生 |
| 全剧改写走 Plan-then-Execute | 当模型上下文 ≥ 1M token 且单次输出可控 → 切回一次性整本输出 |
| 复用单文件 diff | 当剧本拆分为按集多文件 → 接 multi-file diff（next_file / prev_file） |

任一条件命中前不动当前形态。

## 9. 与 task.md 的对齐

| task.md 条目 | 落地 |
|---|---|
| §三-3「真正解决问题的功能」 | 段级 brief 完整化 + 全剧 plan 入口，改写不再是"扔个一句话让 Agent 自由发挥" |
| §三-4「不要重复造轮子」 | Plan-then-Execute 直接套 Cursor Composer / Copilot Workspace 范式；diff 复用现有 Cursor 风格组件 |
| §六「围绕用户决策设计结果」 | 编剧视角的核心决策路径 = 改哪段 → 卡内闭环（hero plan + 段列表 + 行内 dispatch + diff 审阅） |
