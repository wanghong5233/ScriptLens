# ScriptLens 改写 Agent 设计

> 「行动 · 编剧」segment 的执行后端。回答「按某维度改全剧应该怎么改、产出几场、改完什么样」。

## 1. 现状陈述

改写动作收敛到 **全剧维度级 plan-then-execute**，不再保留段级 rewrite_seed。前端入口在 WriterActionCard，提供两路出口：

```
WriterActionCard
├── A 模式：五维一键 (primary 按钮 → fulltext_rewrite plan, dimensions=all-five)
└── B 模式：5 维度独立按钮 (outline → fulltext_rewrite plan, dimensions=[dim])
```

派发链路：

```
WriterActionCard 按钮
    │  AgentTask{ kind:'fulltext_rewrite', mode:'plan', dimensions }
    ▼
dispatchAgentTask (autoSubmit)  ── 用户消息只发一行意图 + <TASK_META> ──┐
    │                                                                    │
    ▼                                                                    ▼
Agent ReAct loop ── propose_dimension_rewrite_tool(mode='plan') ── 出 plan tree
    │                                                                    │
    ▼                                                                    │
RewritePlanCard 渲染 (chat 流内嵌)                                       │
    │  用户勾选 N 场 → 点「执行选中」                                    │
    ▼                                                                    │
dispatchAgentTask (autoSubmit, mode='execute', plan_steps)               │
    │                                                                    │
    ▼                                                                    │
Agent ── propose_dimension_rewrite_tool(mode='execute')                  │
    │  逐场 LLM 改写 → UPDATE scriptlens.scenes.text                     │
    │  mutate state.modified_files / state.original_file_contents        │
    ▼                                                                    │
_generate_file_diffs  ── 检测 script_id → _generate_script_scene_diffs ─┤
    │  从 DB scenes.text 读 modified content（不读磁盘）                 │
    ▼                                                                    │
AgentDiffReview (单文件多 hunk Cursor 风格 · 直接复用) ──────────────────┤
    │  全部 keep 后 closeDiffModal 检测到 contentByPath                  │
    ▼                                                                    │
fe_rescore_hook  ── autoSubmit ── rescore task ───────────────────────────┘
    │  Agent ── score_dimension_tool 逐维度重评 → reply 列「旧分 → 新分」
```

## 2. 分层职责表

| 层 | 负责 | 不负责 |
|---|---|---|
| `WriterActionCard` (前端) | 入口按钮（A/B 双模式）、派发 `fulltext_rewrite` plan 任务、不拼 prompt | 改写文本生成、plan 渲染、执行编排 |
| `RewritePlanCard` (前端) | 渲染 plan tree、勾选子集、派发 `fulltext_rewrite` execute 任务 | 改写文本生成、diff 应用 |
| `AgentDiffReview` (前端) | 单文件多 hunk 内联 diff、行内编辑、Keep/Undo per hunk、prev/next 导航 | 多文件 diff、跨场跳转 |
| `dispatchAgentTask` (前端) | 任务编码、`autoSubmit` 直发、`pendingRescoreRef` 收尾追发 rescore | LLM 调用、UI 渲染 |
| `propose_dimension_rewrite_tool` (后端) | plan/execute 两阶段；execute 模式直 UPDATE DB + mutate AgentState | UI 状态、diff 字符串构造 |
| `rewrite_chain.py` (后端纯函数) | `select_target_scenes` / `propose_plan` / `execute_plan_step`，只跟 LLM 交互 | DB 写入、AgentState 修改 |
| `_generate_script_scene_diffs` (后端) | 把 `state.modified_files` 里的 scene_id 当虚拟文件路径，从 DB 读 modified、从 state 读 original | 改写、scene 持久化 |
| `PUT /api/scripts/{id}/scenes/{scene_id}/content` | reject 路径回写 scene.text（事务 + 用户归属校验） | 改写生成 |
| `score_dimension_tool` (后端) | rescore 阶段逐维度重新打分，写回 `reports.payload` | 改写、计划 |

## 3. 第一性原理

| 维度 | 分析 | 结论 |
|---|---|---|
| 短剧体量 | 单本 ≈ 100 集 × 5 场 ≈ 25–30K tokens | 长上下文模型可吃下，全剧 plan 可行 |
| 痛点性质 | 钩子密度 / 反转齐 / 节奏方差 这类问题是结构性的 | 段级精修只治标，必须按维度全局整改 |
| 输出 token 上限 | 一次输出整本剧本（≈30K out tokens）不可控不可逆 | 必须 plan → execute 两阶段，execute 走逐场改写 |
| 目标场选择 | LLM 自己选目标场容易跑偏 | 后端按 `dim_score < 7` 候选 + 让 LLM 在候选集内决策 |
| Plan 正确性 | LLM plan 可能选错场 / 给错维度组合 | 必须经过用户 review-then-execute；不允许 LLM 直接改场 |
| 文件结构 | 短剧无跨文件；DB 里 `scriptlens.scenes` 一行一场 | 全剧改写产出落同一逻辑文件，复用单文件多 hunk diff |
| 存储介质 | 剧本文本在 PG `scriptlens.scenes.text`，不在文件系统 | scene_id (UUID) 当虚拟文件路径，骗过 `_generate_file_diffs` 的现有契约 |
| Brief 拼装位置 | 早期版本前端拼 800 字 brief 注入 user message，污染 chat 流 | brief 完全后端化，user message 只发一行意图 + `<TASK_META>` |
| Rescore 时机 | 用户接受改写后才有意义；改写前 + reject 路径都不该重评 | 钩在 `closeDiffModal(contentByPath)` keep 路径上，不钩在 reject 上 |
| 业内同构方案 | Cursor Composer plan / Copilot Workspace plan / Devin plan view 全部 review-then-execute | 直接套，不重新发明 |

## 4. AgentTask 契约

```text
AgentTask =
  | EvidenceLookupTask
  | DimInquiryTask
  | FulltextRewriteTask
  | RescoreTask

FulltextRewriteTask {
  kind: 'fulltext_rewrite'
  dimensions: DimensionKey[]            # 五力子集
  mode: 'plan' | 'execute'
  plan_steps?: FulltextRewritePlanStep[] # execute 阶段，从 plan 勾选而来
}

FulltextRewritePlanStep {
  scene_id: string
  target_dimensions: DimensionKey[]
  expected_changes?: string             # ≤120 字
  scene_label?: string
}

RescoreTask {
  kind: 'rescore'
  dimensions: DimensionKey[]
}
```

**不变式**：

- user message 只发一行意图 + `<TASK_META>{...}</TASK_META>`，不在前端拼任何长 prompt
- `autoSubmit:true` 路径不动 composer，用户在 chat 流里看到自己派出去的简短消息
- `fulltext_rewrite execute` 派发后立刻设置 `pendingRescoreRef = dimensions`，由 `closeDiffModal` keep 路径自动消费

## 5. Plan-then-Execute 实装

### 5.1 Plan 阶段

```
propose_dimension_rewrite_tool(mode='plan', dimensions, script_id)
    │
    ▼
rewrite_chain.select_target_scenes(script_id, dimensions)
    │  从 reports.payload 取最新 dim_scores
    │  过滤 dim_score < 7 的场，取 scene.text
    │
    ▼
rewrite_chain.propose_plan(script_id, dimensions, scenes, caller)
    │  LLM 输入：剧本概览 + 角色 + 候选场（带 score / reason / quote）
    │  LLM 输出 JSON：
    │    {
    │      dimensions, overall_summary (≤120 字),
    │      steps: [{scene_id, target_dimensions, rationale, expected_changes, current_excerpt}]
    │    }
    ▼
ToolResult{ data: { mode:'plan', rewrite_plan } }
    │
    ▼
agent_service.handle_agent_response 提取 rewrite_plan
    │
    ▼
chat message meta.rewritePlan = plan
    │
    ▼
RewritePlanCard 渲染：summary + steps（默认全选）+ 「执行选中」按钮
```

### 5.2 Execute 阶段

```
RewritePlanCard.handleExecute → onDispatchExecute(task)
    │
    ▼
dispatchAgentTask(task, {autoSubmit:true})
    │  set pendingRescoreRef = task.dimensions
    │
    ▼
propose_dimension_rewrite_tool(mode='execute', script_id, plan_steps)
    │
    └─ for each step:
       │
       ▼
       rewrite_chain.execute_plan_step(script_id, step, surrounding_scenes, caller)
       │  LLM 输入：剧本上下文 + 目标场 + 前后场 + expected_changes
       │  LLM 输出 JSON：{ rewritten_text, change_summary }
       │
       ▼
       _persist_scene_text(scene_id, new_text, expected_script_id=script_id)
       │  UPDATE scriptlens.scenes SET text WHERE id=:sid AND script_id=:script_id
       │
       ▼
       state.modified_files.add(scene_id)
       state.original_file_contents.setdefault(scene_id, original_text)
```

`rewrite_chain.py` 是纯函数（只跟 LLM 交互，不写 DB、不动 AgentState）；DB 写入和 state mutation 集中在 `propose_dimension_rewrite_tool` 里完成。

## 6. Diff 透明迁移机制

### 6.1 问题

`AgentDiffReview` 早先为 LaTeX 工作区设计，假设 `state.modified_files` 是文件系统路径，`_generate_file_diffs` 直接 `read_text(path)` 取 modified content。剧本文本在 PG `scriptlens.scenes.text`，没文件可读。

### 6.2 决策

把 `scene_id` (UUID) **当作虚拟文件路径**，让现有 diff 链路对调用方透明。前后端没有任何"剧本特殊分支"，只是 `_generate_file_diffs` 在入口分流。

```
state.modified_files = { "<scene_uuid>", ... }      # 不再是磁盘路径
state.original_file_contents = { "<scene_uuid>": original_text, ... }
state.workspace_config.script_id = "<script_uuid>"  # 入口分流标记
```

`_generate_file_diffs` 入口分流：

```python
async def _generate_file_diffs(self, state) -> List[Dict]:
    bound_script_id = state.workspace_config.get("script_id")
    if bound_script_id:
        return self._generate_script_scene_diffs(state, bound_script_id)
    # 否则走原 LaTeX 文件系统逻辑（不变）
```

`_generate_script_scene_diffs(state, script_id)`：

| 字段 | 来源 |
|---|---|
| `file_path` | `scene_id` 字面量（UUID） |
| `original_content` | `state.original_file_contents[scene_id]` |
| `modified_content` | `SELECT text FROM scriptlens.scenes WHERE id=:sid AND script_id=:script_id` |
| `unified_diff / added_lines / removed_lines` | 调用现有 `difflib.unified_diff` 工具 |

### 6.3 Reject 路径

`AgentDiffReview` reject 通过 `PUT /api/scripts/{script_id}/scenes/{scene_id}/content` 把 `original_content` 回写。该端点：

| 检查 | 行为 |
|---|---|
| script 归属用户 | 失败 → 403 |
| `scene.script_id == path script_id` | 失败 → 404 |
| 事务 | `engine.begin()` 包住 UPDATE，`rowcount` ≠ 1 抛 RuntimeError |

### 6.4 不变式

- `AgentDiffReview` 组件签名不动，前端不知道下面是文件系统还是 DB
- `state.modified_files` / `state.original_file_contents` 字段语义不动，只是值的解释由 `workspace_config.script_id` 决定
- 切换到任何新介质（如对象存储）只需新增一个 `_generate_xxx_diffs` 分支 + 一个 PUT 回写端点

## 7. Rescore 链路（fe_rescore_hook）

### 7.1 触发条件

```
keep all 路径： closeDiffModal(paths, contentByPath) ── contentByPath 非空
                                                       │
                                                       ▼
                                       pendingRescoreRef.current 非空
                                                       │
                                                       ▼
                                       autoSubmit rescore task

reject all 路径：closeDiffModal(paths)            ── contentByPath 为 undefined
                                                       │
                                                       ▼
                                       pendingRescoreRef.current = null（仅清空，不派发）
```

判定规则刻意不读 task 类型，只看 close 时是否带 `contentByPath`。reject 路径不带 → 永远不重评。

### 7.2 数据流

```
dispatchAgentTask({kind:'fulltext_rewrite', mode:'execute', dimensions, plan_steps})
    │  pendingRescoreRef.current = dimensions
    ▼
... (Agent 改写 → diff modal → 用户 keep all hunk) ...
    │
    ▼
closeDiffModal(paths, contentByPath)
    │  isKeepPath = !!contentByPath && Object.keys(contentByPath).length > 0
    │  isKeepPath && pendingRescoreRef.current
    │      → setTimeout(0) 内派发 rescore（避开 setFileContent 同步竞争）
    │      → pendingRescoreRef.current = null
    ▼
dispatchAgentTask({kind:'rescore', dimensions}, {autoSubmit:true})
    │
    ▼
Agent ── 按 dimensions 顺序逐维度调 score_dimension_tool
    │
    ▼
Agent reply：「旧分 → 新分」对比表
```

### 7.3 不变式

- `pendingRescoreRef` 只在 `dispatchAgentTask` 派发 `fulltext_rewrite execute` 时被设置
- 一次设置只能被消费一次：keep 路径消费、reject 路径清空，无第三种归宿
- rescore 任务的 dimensions 与触发它的 execute 任务**完全相同**——不能多评、不能漏评

## 8. 可逆性

| 决策 | 切换条件 |
|---|---|
| 用 `scene_id` 当虚拟文件路径 | scene 拆成多文件，或引入跨场 hunk 时切回真正的 multi-file diff |
| `select_target_scenes` 用 `dim_score < 7` 阈值 | 真实数据中候选场覆盖率 > 60% 或 < 5% 时调阈值 |
| Plan 由 LLM 单步生成 | plan 步数 > 30 或单 plan 超出模型 context window 时拆 plan-of-plan |
| Rescore 钩在前端 close 路径 | Agent 内部支持事务级别 callback 时迁到后端 commit 钩子 |
| 用户消息只发一行 + `<TASK_META>` | 出现需要在 prompt 里调试 brief 的场景时切回前端拼装（同时承担 chat 流污染） |

任一条件命中前不动当前形态。

## 9. 与 task.md 的对齐

| task.md 条目 | 落地 |
|---|---|
| §三-3「真正解决问题的功能」 | 改写从段级精修升级到全剧维度级 plan-then-execute，治本不治标 |
| §三-4「不要重复造轮子」 | Diff 机制对剧本场景透明迁移；`AgentDiffReview` / `_generate_file_diffs` / hunk 审阅链路全部复用 |
| §六「围绕用户决策设计结果」 | A 模式一键 / B 模式分维度，决策路径 = 选维度 → 审 plan → 审 hunk → 自动 rescore，全闭环 |
