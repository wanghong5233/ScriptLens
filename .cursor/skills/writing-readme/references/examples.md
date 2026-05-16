# README Examples · 反例 → 正例

## 反例 1：口水话 + 内部实现字段

```markdown
### 证据化报告

为了让分析更准确，ScriptLens 会先调用 `generate_basic_report()`，再把
`EvidenceRef`、`ReportClaim`、`ScoreItem` 这些 Pydantic 字段返回给前端。
具体来说，前端会根据 `evidence_ref_ids` 去查找 `segment_id` 并滚动定位。
```

问题：函数名和 schema 字段是实现细节，"为了 / 具体来说"是口水；公开 README 应先回答用户能得到什么。

## 正例 1

```markdown
### 证据化报告

每个核心判断都可点击回到原文片段，用户能验证主线、冲突、看点、风险和评分依据。
```

## 反例 2：功能表 + 自我夸赞

```markdown
ScriptLens 包含强大的 6 个创新模块：
- Evidence Engine：优雅的证据定位系统
- Perspective Agent：业界领先的多视角分析
- Rewrite Agent：智能改写引擎
- Skill Feedback：革命性的技能进化机制
```

问题：形容词堆砌；模块名像营销词，读者仍不知道如何体验。

## 正例 2

```markdown
| 能力 | 用户能做什么 |
|---|---|
| 证据化报告 | 点击结论定位原文片段 |
| 多视角分析 | 切换选品、编剧、投放、审核视角 |
| 定向改写 | 从低分项进入局部改写建议 |
| 反馈 Skill | 保存偏好并影响后续追问 |
```

## 反例 3：失效链接 + 内部目录

```markdown
> Demo: https://old-scriptlens-demo.example.com （旧地址，404）
>
> 部署细节见 [docs/private/deploy-notes.md](./docs/private/deploy-notes.md)
```

问题：旧 demo 链接已废弃；`docs/private/` 是个人材料，不应作为公开入口。

## 正例 3

```markdown
> Demo: https://scriptlens.example.com
>
> 本地启动见 [docs/10-tech-stack-decision.md](./docs/10-tech-stack-decision.md)
```

## 子 README 误写 vs 正确归宿

| 子 README 误写 | 正确归宿 |
|---|---|
| 复述项目愿景 / 整体架构 / 功能特性 | 删，引根 README |
| 描述与根 README 重叠的 Tech Stack | 删 |
| 子目录独有的开发命令（`uvicorn` / `npm run dev`） | 保留 |
| 子目录独有的环境变量 / 目录约定 | 保留 |
| 子目录独有的验收命令（`pytest` / `make eval`） | 保留 |

**判别口诀**：删掉这段后，根 README 仍能让人启动起来吗？能 → 子 README 该写它；不能 → 它属于根 README。
