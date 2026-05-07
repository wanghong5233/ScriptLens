# ScriptLens 行动 Lens（Persona Action Card）

> 报告右栏 5 segment 的最末一段。前 4 段（速览 / 故事 / 人物 / 评估）回答「这本剧本是什么」，本段回答「基于这份剧本和我的角色，我现在该做什么」。

## 1. 现状陈述

报告右栏 segment 顺序固定为：

```
速览 → 故事 → 人物 → 评估 → 行动
```

「行动」segment 同时渲染三张 Persona Action Card（选品 / 编剧 / 审核），不互斥、不切 tab。每张卡 = 一句话结论 + 优先证据 ≤3 + Next Action ≤3。

数据流向：

```
ReportPayload (后端持久化)
    │  scorecard / compliance / rewrite_seeds / evidence_refs / decision
    ▼
ViewResponse (router 透传，无 role 参数)
    ▼
ActionSegment (前端纯派生)
    ▼
[选品卡 | 编剧卡 | 审核卡]
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
| 业内对照 | Tableau Persona Dashboard / Linear Roadmap Views / Airbnb 角色仪表盘均采用 action card pattern | 直接套用 |
| 实现成本 | 新增 segment 全部 derived，无后端 LLM 调用，无 schema 扩展 | 一次原子前端改动可落地 |
| 可逆性 | 派生逻辑写死在前端组件 | 切回后端派生只需新增 `view/actions` 接口，不影响 ReportPayload schema |

## 4. Persona Action Card 契约

每张卡 4 块固定结构，渲染顺序不可调换：

| 块 | 字段 | 数据源 |
|---|---|---|
| ① 一句话结论 | `verdict: string ≤30字` + `confidence: high\|medium\|low` | derived from `decision.label / overall_score / compliance.level` |
| ② 优先证据 ≤3 | `evidence: { dimension, score, scene_label, scene_id, quote ≤90字 }[]` | 按角色优先维度从 `scorecard.evidence_ref_ids` 中挑 |
| ③ 关键提示 | 角色相关 1 条提示（题材徽章 / OOC 警示 / 红线列表） | 各角色不同字段，详见 §4.1-4.3 |
| ④ Next Action ≤3 | `actions: { label, kind, payload }[]` | 静态配置 + `dispatchTask` |

### 4.1 选品卡

| 块 | 内容 |
|---|---|
| ① 结论 | `verdict` = `decision.label` 中文映射（推荐立项 / 审慎推进 / 先判断不值得继续读）；`reason` 复用 `decision.one_sentence_reason` |
| ② 优先证据 | 维度优先序 `concept → emotion → story`，各取首条 `evidence_ref` |
| ③ 关键提示 | 题材徽章 `coverage_card.genre` + `overall_score` + `compliance.level`（仅 `clean` 之外显示） |
| ④ Next Action | `[追问: 同类爆款抖音表现, 导出选品报告 PDF]` |

### 4.2 编剧卡

| 块 | 内容 |
|---|---|
| ① 结论 | 由 `rewrite_seeds.length` 派生：≥2 → 「N 段需重写」/ 1 → 「1 段建议优化」/ 0 → 「整体可保留」 |
| ② 优先证据 | 直接复用 `rewrite_seeds[:3]`，每条带维度 + `scene_label` + `issue` |
| ③ 关键提示 | OOC 警示，从 `evaluation.dimensions.character.reason` 抽取一句（无则降级为「主角动机弧光稳定」） |
| ④ Next Action | `[一键改写最低分段, 追问: <最低分维度> 的具体问题, 跳转故事 segment 看节奏曲线]` |

### 4.3 审核卡

| 块 | 内容 |
|---|---|
| ① 结论 | 由 `compliance.level` 派生：`clean` → 「过」/ `low_risk` → 「修改后过」/ `medium_risk` → 「修改后过（需复审）」/ `high_risk` → 「退回」 |
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
