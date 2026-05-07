# ScriptLens 系统心智模型

> 本文档是 [`01-requirements.md`](01-requirements.md) **UI 与 Agent 协作落地形态的最新认知**。
> 当 PRD 的契约（schema / API / 5 维评分依据）与本文表述冲突时，**契约以 PRD 为准；交互形态与协作关系以本文为准**。
> 本文随开发演进迭代，PRD 保持稳定。

## 1. 这份文档解决什么

PRD 写明了「契约层」（5 维评分依据、`ReportPayload` schema、API 协议）。但**契约 → UI/Agent 的具体协作形态**在开发中演进多次（详见 §10 演进记录）。本文固化最新心智，避免开发中再次漂移到「报告作为孤立结果页 + Agent 作为独立聊天框」的错误模型。

## 2. 第一性原理（task.md 抽出，不动）

| 原文 | 系统含义 |
|---|---|
| 「理解」≠ 摘要 ≠ 复述 = **把影响判断的信息提取 + 组织 + 定位 + 解释** | 任何结论必须能回到原文锚点 |
| 「保留原文依据 / 让用户能快速定位到某个关键部分」 | 报告里所有可点元素必须能跳到原文 + 高亮 |
| 「用户可以要求查看某个判断的依据 / 要求改写」 | 溯源、改写都是 **用户主动发起的任务**，不是预生成结果 |
| 「真正可工作的 Agent，不是概念方案」 | 改写文本由实时 ReAct Agent 生产，不在报告里预生成 |
| 「避免只做表面润色」 | 改写需带上下文（前后场 + 人物关系 + 用户偏好），预生成必然表面化 |

## 3. 系统全貌

```
┌─────────────────────────────────────────────────────────────────────┐
│                       ScriptLens doc-studio                          │
│                                                                       │
│  ┌──────────┐  ┌──────────────────┐  ┌────────────────────────────┐ │
│  │ 左：大纲  │  │ 中：原文编辑器    │  │ 右栏（三 tab）              │ │
│  │           │  │ Monaco + decoration│  │ ① Agent 对话(执行器)        │ │
│  │ 集→场     │←→│ 行级证据高亮     │←→│ ② 分析报告(任务派发器)      │ │
│  │           │  │ Ctrl+L 选区注入  │  │ ③ 时间线(timeline)          │ │
│  └──────────┘  └──────────────────┘  └────────────────────────────┘ │
│                                                                       │
│  ── 数据流：报告 ──dispatchTask──→ 编辑器高亮 + Agent 对话注入 ──────│
│  ── 反向流：Agent 改写完成 ── script_operations ──→ 报告状态徽章 ────│
└─────────────────────────────────────────────────────────────────────┘
```

## 4. 三角色分工

| 模块 | 角色 | 已实装能力 | 来源 |
|---|---|---|---|
| **Agent 对话**（右栏 tab ①） | 任务执行器 | ReAct 循环 / 工具栈 / Ctrl+L 选区注入 / AgentDiffReview / 多 session / 自由提问 | ScholarMind doc-studio 蒸馏自 Cursor，复用 |
| **分析报告**（右栏 tab ②） | 任务派发器 + 决策枢纽 | 4 segment（速览 / 故事 / 人物 / 评估）+ 任务状态徽章；结构契约见 [`05-report-architecture.md`](05-report-architecture.md) | ScriptLens 新增 |
| **时间线**（右栏 tab ③） | 回写存储 | `script_operations` 表 + 前端 timeline | ScholarMind 复用 |

**核心约束**：报告**只产候选 + 触发**，**不产改写文本**；改写文本由 Agent ReAct 实时生产并落入 timeline，再回写到报告状态徽章。

## 5. 进入 Agent 对话的两条路径（并存，互不干扰）

| 路径 | 触发方式 | session 行为 | 实装 |
|---|---|---|---|
| **A · 任务派发** | 报告里点证据 / 改写候选 / 关键场景 | 默认追加到当前 session（持续追问连贯，PRD §5 Q3）；用户可"+ 新建对话"显式开新 session 隔离 | 本轮新增 |
| **B · 自由提问** | 中央编辑器 Ctrl+L 选区 / 直接输入 / "+ 新建对话" | 完全自由（Cursor 模式） | 已实装，零改动 |

**心智锚**：Cursor 也是这两条 —— 点 Problems 面板 quick fix 是 A，⌘L 选区注入 chat 是 B。

## 6. AgentTask 协议（统一通信抽象）

```ts
type AgentTask =
  | { kind: 'evidence_lookup';  evidence_ref_id; scene_id; scene_label; start_line?; end_line? }
  | RewriteSeedTask
  | { kind: 'dim_inquiry';      dimension; current_score }

// rewrite_seed 是动作族里唯一字段较多的 kind，详见 docs/10-rewrite-agent.md §4
interface RewriteSeedTask {
  kind: 'rewrite_seed'
  dimension: 'story' | 'character' | 'concept' | 'emotion' | 'pacing'
  scene_id: string
  scene_label?: string | null
  issue: string
  evidence_ref_id: string
  // 完整 brief：Agent 改写时必须吃到原文 + 元数据，缺失时 prompt 静默降级
  score?: number | null
  dim_reason?: string | null
  quote?: string | null
  episode_no?: number | null
  scene_no?: string | null
  genre?: string[] | null
  overall_score?: number | null
  decision_label?: string | null
}
```

`dispatchTask(task)` 干三件**原子操作**：

1. **联动编辑器**：`openFile(scene_id)` → `revealLines(start_line, end_line)` → Monaco decoration 行高亮 3 s 淡出
2. **切右栏 tab**：自动切到「Agent 对话」
3. **注入 composer**：人类可读 prompt + `<TASK_META>` JSON block（**不自动 send**，让用户检查 / 追加偏好后 Enter，符合 Cursor 习惯）

**Agent 后端约定**：消息里识别到 `<TASK_META>{...}</TASK_META>` 时，按 JSON 字段直调工具，**跳过模糊定位**：

- `kind=evidence_lookup` → `cite_evidence_tool({scene_id, evidence_ref_id})`
- `kind=rewrite_seed` → `propose_rewrite_tool({scene_id, dimension})`
- `kind=dim_inquiry` → 维度专家追问 prompt + 必要时 `web_search_tool`

省一轮 ReAct = 用户感知响应快 + 不会因 LLM 检索误差跳到错的场。

## 7. 端到端数据流：报告点击 → 改写采纳 → 状态回写

```
点证据 chip                           点改写候选                          点关键场景
   │                                    │                                  │
   ▼                                    ▼                                  ▼
{kind: evidence_lookup}        {kind: rewrite_seed}              {kind: evidence_lookup}
            │                              │                                │
            └──────────────┬───────────────┴────────────────────────────────┘
                           ▼
                    dispatchTask(task)
                           │
        ┌──────────────────┼─────────────────────┐
        ▼                  ▼                     ▼
 编辑器跳转高亮      切到 Agent tab          composer 注入 prompt
                                                  │
                                          [用户 Enter 或追问]
                                                  ▼
                                         Agent ReAct 循环
                                         ├─ locate_scenes_tool（带 scene_id）
                                         ├─ propose_rewrite_tool → diff
                                         └─ AgentDiffReview（接受/拒绝）
                                                  │
                                              (接受时)
                                                  ▼
                                  script_operations 写一条 op
                                  (target_dimension + scene_id)
                                                  │
                                                  ▼
                          报告刷新 → 卡片徽章变成"已采纳改写"
```

## 8. 任务状态回写（零 schema 改动）

`script_operations` 表已有 `target_dimension`、`modified_files`（含 scene_id）字段。`script_view_service` 在拼装 view 时按 `(scene_id, target_dimension)` group by 派生：

```ts
type RewriteTaskStatus = {
  attempts: number              // 该 (scene, dim) 上的改写次数
  last_op_id: string | null     // 最近一次改写 op，前端可跳 timeline
  last_status: 'proposed' | 'accepted' | 'rejected' | null
  last_at: string | null        // ISO 时间
}
```

**徽章映射**：

| 状态 | 徽章 | 颜色 |
|---|---|---|
| `attempts=0` | 未处理 | 灰 |
| `last_status=proposed` 或 `attempts>0 且 accepted=0` | 已尝试 N 次 | 蓝 |
| `last_status=accepted` | 已采纳改写 | 绿 |
| `last_status=rejected`（仅最近一次） | 上次拒绝，可重试 | 橙 |

## 9. 报告位置决策

| 选项 | 结论 | 理由 |
|---|---|---|
| 报告作为独立路由 `/scripts/:id/report` 主入口 | ❌ 降级为「全屏阅读 / 分享 / 打印」备用 | 离开了原文 + 编辑器，证据不可点 = 不可溯源，违反 §3 数据流 |
| 报告作为 doc-studio 右栏 tab ② 主入口 | ✅ 默认入口 | 与原文同屏，dispatchTask 能直接联动编辑器；右栏宽度可容纳决策卡 + 5 维卡 + 必读 3 场。改写候选已迁移到「行动 · 编剧」segment（详见 docs/10-rewrite-agent.md §1） |
| 删除独立路由 | ❌ 保留 | 全屏沉浸阅读、外部分享链接仍有用；独立页里的可点元素跳回 doc-studio 带 `?task=base64(...)` 通道 |

## 10. 不做的（YAGNI 边界）

| 不做 | 替代方案 | 理由 |
|---|---|---|
| LLM 在 `generate_report` 里预生成 `rewritten_excerpt` | 报告只产 `rewrite_seeds`（候选定位），改写文本由 chat 实时跑 | Cursor / Copilot / Notion AI / Grammarly 共识：改写应按需 + 上下文 + 可迭代；预生成 90% 浪费且必然表面润色 |
| `ReportPayload` schema 加 `rewrite_suggestions` 字段 | 在 `ScriptViewResponse` 派生 `rewrite_seeds`，PRD §7 schema 不动 | 持久化层稳定，派生字段在视图层 |
| 强制 1 task = 1 session | 默认追加，可显式新建 | 与 Cursor Composer 一致，连续追问更自然 |
| 全部废弃独立 report 页 | 保留作为备用入口 | 全屏阅读 / 外链分享场景仍存在 |

## 11. 演进记录（决策为什么是这样）

| 时间 | 决策 | 触发原因 |
|---|---|---|
| 初版 | 报告独立路由 + 5 维卡片只显示分数 | 复用 ScholarMind 报告页骨架，未深思溯源 |
| v1 修正 | rail 在 doc-studio 右栏，但仍只显示 mini scorecard，证据/必读/改写候选缺失 | 用户反馈：「无法溯源 / 没有改写建议」 |
| v2 | 报告 = 任务派发器，统一 AgentTask 协议，复用已实装 ReAct + Diff + Timeline | 用户洞察：「不要孤立考虑，这是一个系统」「Agent 对话已经是 Cursor 模式蒸馏品，报告应作为决策入口」 |
| v2.1 hotfix | dispatchTask 用 `findSceneById` 替换 `findSceneByRef`；evidence 行号改为 `scene.text` 物理 1-indexed；quote 跳过人物清单/场景头/位置行 | 用户反馈："溯源根本找不到东西"——实测发现 ① UUID 走场号匹配必失败 ② start_line 是 paragraphs 数组下标，与 Monaco 打开的 scene.text 行号坐标不一致 ③ quote 命中"人物：xxx" 这种结构性元数据行 |
| v2.1 prompt | 重写 `buildPromptFromTask`：去 `[任务]/[场景]/[问题]` 学术腔，去 scene_id UUID 噪声，改第一人称口语化短模板 | 用户反馈："你这段提示词也是一坨狗屎"——派发到 chat 的 prompt 应是用户起手模板，不是替 Agent 预写的指令 |
| v2.1 embedding | `SCRIPTLENS_USE_EMBEDDING` flag，默认关闭；ingestion 跳过向量写入，retrieve_scenes 退化为纯 BM25 | 用户质疑："我们这个规模需要 embedding 吗？"——自查发现 evaluator/evidence_refs/任务派发三条链路都不查向量，唯一调用方是 `locate_scenes_tool`，而 BM25 + 元数据过滤已经够用 |
| v3 报告重构 | 报告改为 4 segment（速览 / 故事 / 人物 / 评估）；新增 `coverage_card` / `beat_sheet` / `character_graph` / `pacing_curve` / `evaluation` 5 个并行 chain；5 维评分降级到「评估」segment；关键场从 beat_chain 锚点反推；详见 [`05-report-architecture.md`](05-report-architecture.md) | 用户反馈："关键场景出『电视上放着猫和老鼠』全是垃圾"——根因是关键场来自 5 维评分证据，非故事节拍；同时报告以分数主导而非决策主导，违反 task.md §六「围绕用户决策设计结果，不是围绕文本做机械摘要」 |
| v3 存储评估 | DB 保留 PostgreSQL（与 ScholarMind compose 共部署）；Agent 检索加 jieba 关键词兜底；SQLite + FTS5 作为未来优化点，详见 [`06-storage-architecture.md`](06-storage-architecture.md) | 实际看代码后 SQLite 迁移涉及 15+ 文件 PG-only SQL，1-1.5 天工作量与 task.md 主线无关；当前主线是报告质量、可溯源、可追问、可改写 |

## 12. 相关文档

- [`source/task.md`](source/task.md) · 题目原文，最高准则
- [`01-requirements.md`](01-requirements.md) · PRD（task.md 工程化落地基线）
- [`02-script-evaluation-rubric.md`](02-script-evaluation-rubric.md) · 5 维评分工业判据 + prompt 模板
- [`04-script-pipeline.md`](04-script-pipeline.md) · 剧本数据流水线（上传 → 评分 → 检索 → 派发）
- [`05-report-architecture.md`](05-report-architecture.md) · 报告 4 segment 结构契约 + 数据契约
- [`06-storage-architecture.md`](06-storage-architecture.md) · 存储层（PG 当前实现 + SQLite/FTS5 演进方向）
- [`07-evaluation.md`](07-evaluation.md) · 解析质量评估方法（人工标注 + 自动指标 + 用户任务）
- [`00-reuse-matrix.md`](00-reuse-matrix.md) · ScholarMind 模块复用矩阵 + web_search 边界
