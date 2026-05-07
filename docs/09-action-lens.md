# ScriptLens 行动 Lens（Persona Action Card）

> 报告右栏 5 segment 的最末一段。前 4 段（速览 / 故事 / 人物 / 评估）回答「这本剧本是什么」，本段回答「基于这份剧本和我的角色，我现在该做什么」。

## 1. 现状陈述

报告右栏 segment 顺序固定为：

```
速览 → 故事 → 人物 → 评估 → 行动
```

「行动」segment 内置局部视角切换（Segmented control），同一时刻仅渲染一张 Persona Action Card：

- 默认 `writer`（编剧）——ScriptLens 最高频深度用户，判断短决策（签 / 过）由选品 / 审核异步触发。
- 切换到 `selection` / `review` 时整张卡的结论 / 证据 / Next Action 完全替换，非顺序变化。

每张卡 = 一句话结论 + 优先证据 ≤3 + 关键提示 1 条 + Next Action ≤3。

数据流向：

```
ReportPayload (后端持久化)
    │  scorecard / compliance / rewrite_seeds / evidence_refs / decision
    ▼
ViewResponse (router 透传，无 role 参数)
    ▼
ActionSegment (前端，内置 persona 状态，默认 writer)
    ▼
当前 persona → 选品卡 / 编剧卡 / 审核卡（三选一）
    ▼
dispatchTask (追问 Agent / 一键改写 / 导出)
```

## 2. 分层职责表

| 层 | 负责 | 不负责 |
|---|---|---|
| `ReportPayload` (后端) | 五力评分 / 合规分级 / 改写候选 / 必读场 / 决策卡 | 角色重排 / 视角过滤 |
| `ActionSegment` (前端) | 派生三张 Persona Action Card / Next Action 按钮组装 | LLM 调用 / 数据计算 |
| `dispatchTask` hook | Next Action 触发 Agent 任务（追问 / 改写 / 导出） | 数据派生 / UI 状态 |

## 3. 第一性原理

| 维度 | 分析 | 结论 |
|---|---|---|
| 数据子集 | 三角色关注的核心字段 100% 重叠（决策卡 / 合规等级 / 低分段都共看） | 视角过滤数据无业务空间，不做 |
| 视觉差异 | 全局 tab 切换仅引发 5 张维度卡顺序变化与 must_read 3 条更替，其它 14 个 widget 不变 | 用户感知 ≈ 0，删除 |
| 决策路径 | 选品 = 签不签 / 编剧 = 改哪段 / 审核 = 过不过；路径互不重叠 | 视角形态应是 action card，不是 data view |
| 同屏 vs 切换 | 同屏：三张卡总高 ≈ 720px 顶替整段，注意力被三向稀释；切换：默认 writer 单卡 ≈ 240px，专注度 1×。Notion / Linear / Tableau Persona Dashboard 均采单视图切换 | 局部 Segmented 切换，单次一张卡 |
| 默认视角 | 短剧链路高频读者是编剧（逐段啃稿），选品 / 审核是判断短决策（30 秒以内） | 默认 `writer`，其他 persona 切换显示 |
| 业内对照 | Tableau Persona Dashboard / Linear Roadmap Views / Airbnb 角色仪表盘均采用 action card pattern | 直接套用 |
| 实现成本 | 新增 segment 全部 derived，无后端 LLM 调用，无 schema 扩展 | 一次原子前端改动可落地 |
| 可逆性 | 派生逻辑写死在前端组件 | 切回后端派生只需新增 `view/actions` 接口，不影响 ReportPayload schema |

## 4. Persona Action Card 契约

每张卡 4 块固定结构，渲染顺序不可调换。

**字段来源约束（强制）**：

| 字段 | 允许来源 | 禁止 |
|---|---|---|
| `badge` | 枚举值的中文 i18n（`decision.label` / `compliance.level` / 计数分级） | 自由话术 |
| `verdict` | 数据计数（`rewrite_seeds.length`）/ 状态映射（`compliance.level`） | 前端拼接的描述句（如"前 5 集抓人"），凡含具体内容描述必须来自 LLM |
| `reason` | 后端 LLM 输出（`decision.one_sentence_reason` / `compliance.reason`） | 前端模板字符串 |
| `evidence` | `evidence_refs` / `rewrite_seeds` 直接引用 | 前端再合成 quote |
| `actions` | 静态枚举（导航 / `dispatchTask`） | 含动态文本的按钮标签 |

业内对照：番茄选品后台 / 阅文评估卡 / 抖音/快手短剧选品后台 / 内容安全审核工单均采「枚举 tag + LLM 自由理由文本」二元结构，无写死的描述话术中间层。

| 块 | 字段 | 数据源 |
|---|---|---|
| ① 一句话结论 | `badge` + 可选 `verdict`（仅有数据派生依据时） + `reason`（LLM 输出，可选） | derived from `decision.label` / `compliance.level` / `rewrite_seeds.length`，**不允许前端拼模板话术** |
| ② 优先证据 ≤3 | `evidence: { dimension, score, scene_label, scene_id, quote ≤90字 }[]` | 按角色优先维度从 `scorecard.evidence_ref_ids` 中挑 |
| ③ 关键提示 | 角色相关 1 条提示（题材徽章 / OOC 警示 / 红线列表） | 各角色不同字段，详见 §4.1-4.3 |
| ④ Next Action ≤3 | `actions: { label, kind, payload }[]` | 静态配置 + `dispatchTask` |

### 4.1 选品卡

| 块 | 内容 |
|---|---|
| ① 结论 | `badge` = 产品定性（推荐立项 / 审慎推进 / 不建议立项），来自 `decision.label` 中文映射；`reason` 直接渲染 `decision.one_sentence_reason`，由后端 LLM 基于五力评分 + 题材 + 合规综合给出。**禁止前端拼模板话术作为 verdict**——业内对照（番茄选品后台 / 阅文评估卡 / 抖音/快手短剧选品）均采 `tag + 自由文本理由` 二元结构，不存在写死的"签 · 前 5 集抓人"这类描述句 |
| ② 优先证据 | 维度优先序 `concept → emotion → story`，各取首条 `evidence_ref` |
| ③ 关键提示 | 题材徽章 `coverage_card.genre` + `overall_score` + `compliance.level`（仅 `clean` 之外显示） |
| ④ Next Action | `[追问: 同类爆款抖音表现, 导出选品报告 PDF]` |

### 4.2 编剧卡

编剧卡形态与选品 / 审核卡不同——它是「双层动作面板」而非「结论卡」。详细规约见 `docs/10-rewrite-agent.md`，本节只摘要：

| 块 | 内容 |
|---|---|
| ① badge | 由 `rewrite_seeds.length` 派生：≥5 → 「N 段需重写」/ ≥1 → 「N 段建议优化」/ 0 → 「整体可保留」 |
| ② Hero · 全剧改写计划 | primary 入口 + 五力短板（< 6 的最低 3 维）一句话 + Plan-then-Execute hint。Step 1 stub `message.info` 提示，Step 2 接 `fulltext_rewrite` task |
| ③ 段级精修列表 | `rewrite_seeds` 完整渲染（不限 3 条），每行 = dim tag + score + 集场坐标 + issue + 「Agent 改这段」+「先看原文」行内按钮。派发 `rewrite_seed` 时填完整 brief |
| ④ 底部次级动作 | `[追问 · 最低分维度怎么改, 看完整节奏曲线 → story segment]` |

不复用 `ActionCardShell`——编剧卡的 hero + 列表 + 行内按钮诉求与"结论卡"模式正交。

### 4.3 审核卡

| 块 | 内容 |
|---|---|
| ① 结论 | 双轴标注（业内审核后台标准）：`badge` = 风险等级（安全 / 低风险 / 中风险 / 高风险），`verdict` = 处置动作（过审 / 修改后过 / 修改后过 · 需复审 / 退回 · 不建议立项），均由 `compliance.level` 状态映射。`reason` 渲染 `compliance.reason`（后端 LLM 输出） |
| ② 优先证据 | `compliance.violations[:3]`，每条带 `scene_label` + 命中关键词 + 严重度 |
| ③ 关键提示 | 合规分 + 红线条数 + 涉及集数 |
| ④ Next Action | `[导出风险清单 CSV, 追问: 这条是否违反广电细则, 跳转评估 segment 看完整列表]` |

合规违规不进 `rewrite_seeds`——红线问题人工复审，LLM 不下场改。

## 5. 接口契约

```text
GET /api/scripts/{id}/view  →  ViewResponse
```

**不变式**：

- 不接受 `?role=` 参数
- `scorecard` 顺序固定为维度声明序（story / character / concept / emotion / pacing），不按角色重排
- `must_read_scene_ids` 为全局必读，与角色无关
- 三张 Persona Action Card 在前端 derived，后端不返回 card 结构
- 任一字段缺失（如 `compliance` 为 `null`）时对应卡降级为骨架占位 + 一句话原因，不抛错

## 6. 可逆性 / 重评触发条件

当前 derived-on-frontend，切换到 backend-derived 的触发条件：

1. Persona Card 任一字段需要 LLM 二次推理（如「为什么这条违反广电细则」的具体引用）
2. Persona 配置数 ≥ 5，前端模板维护成本超过后端 API
3. 第三方接入需要无前端获取 Card 数据

任一条件命中前不动后端。

## 7. 与 task.md 的对齐

| task.md 条目 | 落地 |
|---|---|
| §三-2「保留原文依据」 | 每张卡的优先证据均带 `scene_id` 跳原文 |
| §三-3「什么情况下用户觉得有用」 | 三张卡每张直接给 Next Action 按钮 |
| §三-4「什么功能才真正解决问题」 | 删除全局视角 tab（数据 lens 假象），改 action lens |
| §六「围绕用户决策设计结果」 | 三张卡分别对应三个角色的核心决策路径 |
| §五-5「视角切换」加分项 | 由 action card 形态实装，不再是 tab 重排 |
