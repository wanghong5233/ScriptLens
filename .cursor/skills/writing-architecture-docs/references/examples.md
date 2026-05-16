# Architecture Doc Examples · 反例 → 正例

## 反例：结论先行 + 辩证过程 + 工件清单

```markdown
### B.1 结论先行
当前阶段，ScriptLens 放弃复杂多 Agent 编排……

### B.2 决策背景
之前我们考虑过直接长上下文、向量数据库、分层抽取三种路线。

### B.4 我们的辩证过程
1. 第一反应（错）：一次 prompt 生成全部报告
   - 反驳：证据定位和多轮追问会漂移

### B.7 已删除/不再维护的工件
- old_report.py
- draft_prompt_v0.md
```

问题：

- 结论先行的"当前阶段……"是 TL;DR 元结构，纯污染
- 决策背景 / 辩证过程是过去式叙事，不是架构事实
- 工件清单是 git log 的职责

## 正例：现状陈述 + 维度表

```markdown
## B.1 架构

ScriptLens 采用 FastAPI + Next.js。后端按 ingest、segmentation、reporting、evidence、chat、rewrite、feedback、evaluation 切分能力；前端以左原文、右报告的方式展示可验证分析。

[mermaid 图]

## B.2 第一性原理

| 维度 | 分析 | 结论 |
|---|---|---|
| 数据规模 | 长剧本信息分散，必须保留原文定位 | 分段和 evidence refs 是底座 |
| 能力归属 | 代码负责证据、契约、失败边界；LLM 负责生成候选文本 | 输出必须过 schema |
| 写入成本 | 10 天考核更需要可演示闭环 | 不引入重型向量库作为硬依赖 |
| 可逆性 | 模型、部署和存储后续可替换 | provider 和 schema 解耦 |
```

（工件清单归 git log / CHANGELOG，不进架构文档。）

## 章节形态示例

### 现状陈述

> ScriptLens 采用 Web App + FastAPI Agent backend，围绕长剧本输入、证据化报告、多轮追问、定向改写、反馈 skill 和评估闭环提供能力。

一句话 + 一图（mermaid 或分层职责表）。

### 分层职责表

| 层 | 负责 | 不负责 |
|---|---|---|
| Web App | 上传、原文阅读、报告、证据、视角、追问、改写、反馈 | 业务判断 |
| API Backend | HTTP API、错误边界、能力编排 | 页面布局 |
| Ingest / Segmentation | 文本/PDF 提取、清洗、分段、行号 | 剧情判断 |
| Reporting / Evidence | coverage、scorecard、claims、evidence refs | 泛泛摘要 |
| Chat / Rewrite / Feedback | 基于证据追问、低分改写、临时 skill | 无证据强答 |
| Evaluation | fixtures、golden、release gate | 绝对客观评价 |

三列 `层 / 负责 / 不负责`，无解释段落。

### 第一性原理维度选项

- **数据规模**（量化：行数、tokens、样本数）
- **能力归属**（哪个角色负责这件事）
- **写入/读取成本**（延迟、token、依赖体积）
- **故障域**（失败面、传染性）
- **可逆性**（未来换方案的迁移成本）

### 接口契约

```text
POST /api/scripts -> ScriptCreateResponse
POST /api/scripts/{script_id}/analyze -> ScriptReport
POST /api/scripts/{script_id}/chat -> ScriptChatResponse
POST /api/scripts/{script_id}/rewrite -> RewriteSuggestion
POST /api/scripts/{script_id}/feedback -> FeedbackResponse
```

**不变式**：
- 返回结构必须符合 Pydantic schema
- 剧本分析结论必须能回到 `EvidenceRef`、source segment、fixture/golden label 或用户反馈
- 无法解析模型输出、外部 API 鉴权失败、PDF 提取失败、证据定位失败时失败，不返回伪成功

### 可逆性 / 重评触发条件

"当前选 A，未来可能换 B"类决策给量化门槛：

1. 单剧本上下文超过主模型稳定窗口
2. 单次分析 p95 超过 demo 可接受延迟
3. 证据定位失败 case ≥ 5 起
4. 评估样本覆盖不到新增输入类型

**触发判断以运行时指标为准，不在无数据时提前决策。**
