---
name: writing-issue-backlog
description: Write or refactor known-issues/backlog tracker (problem-first, evidence-before-solution). Use when editing `docs/KNOWN_ISSUES_AND_BACKLOG*.md`, or user asks to 记录 bug/补 issue/整理待办/复审 issue, or complains an entry has 拍脑袋的解法/补丁式方案/启发式/先入为主. Do NOT use for postmortems or pitfall archive.
---

# Writing Issue & Backlog Tracker

## 一句话准则

**只追踪未解决的问题，让"问题本身"先讲清楚。Root Cause 未定之前的"下一步=方案"都是噪音。**

## 与其它"问题类文档"的边界

| 文档 | 范围 | 时态 | 位置 |
|---|---|---|---|
| **Known Issues & Backlog** | 活跃、未解决 | 现在时：还在发生 | **`docs/KNOWN_ISSUES_AND_BACKLOG.md`** |
| Pitfall Archive | 已收敛、需防复发的不变量 | 现在时：现在的规则是 | `docs/*MANUAL*.md` |
| Postmortem | 单次事件复盘 | 过去式 | git commit / `private/` |
| Engineering Playbook | 跨项目工程直觉 | 中性 | `docs/private/engineering-playbook/` |
| ADR / RFC | 长期决策 | 现在时 | `docs/*ADR*` |

判别口诀：「还没解决」→ 本文件；「已解决防复发」→ pitfall；「跨项目复用」→ playbook；「定长期约束」→ ADR。

## 硬性禁止（命中即拒收）

| 反模式 | 判断特征 | 归宿 |
|---|---|---|
| 先写解法、再补问题 | "下一步"早于或长于"问题描述/Evidence" | 重写按 §字段顺序 |
| 启发式补丁式表达 | "默认走 X / 自动 Y" 未经评审 | 改为 Open Question 或拆 ADR |
| Bug / 改进 / 验证混为一谈 | 三类问题套同一模板 | 按 `type` 分流（见 §字段契约） |
| 不可复现的 Bug 进入活跃列表 | 无 Repro / 无 Evidence | 标 `triaging`，30 天补不齐就关闭 |
| 同根因拆多 ID | 两条 Issue 指向同一 root cause | 合并到首个 ID，其余删 |
| 已关闭条目残留 | "已上线 / 已修复" | 直接删（git 保留历史） |
| 占位语言 | "TBD / 待定 / 后续会处理" | 删，或改 `Open Question` |
| 30+ 行日志全文 | 完整 traceback | 留关键 3 行 + 链接 |
| 模糊量化 | "很慢 / 经常失败" | 量化：`p95 12.4s` / `30%/周` |

详见 `references/design-rationale.md` 解释 why。

## 状态机（仅 4 个合法值）

| 状态 | 含义 | 进入 → 离开 |
|---|---|---|
| `triaging` | 问题未定义清楚 | 新报告 → Symptom/Repro/Evidence 齐 → `investigating` |
| `investigating` | 已定义，根因未定 | triaging 完成 → Root Cause 写明 → `planned` |
| `planned` | 根因已定，待实现 | investigating 完成 → 实现合并 → 移出文件 |
| `blocked` | 等外部输入 | 任何阶段被阻塞 → 阻塞解除 → 回原阶段 |

禁用：`todo` / `done` / `in_progress` / `wip` / `wontfix`（含义模糊）。

## Issue 类型与字段契约（核心）

每条 Issue 先声明 `type`，按对应字段集顺序写。**字段顺序即写作顺序**。

### 类型 A · Bug（行为错误）

| # | 字段 | 含义 |
|---|---|---|
| 1 | `Symptom` | 现象，1-2 句不解释 |
| 2 | `Repro` | 他人可原样执行的最小步骤 + 环境 |
| 3 | `Observed Evidence` | 日志 / payload / 状态码原文，2-4 行 |
| 4 | `Scope` | 影响范围 + 版本 |
| 5 | `Impact` | 业务/用户后果，量化 |
| 6 | `Hypotheses` | 编号、互斥、可证伪 |
| 7 | `Open Questions` | 每条可被一次实验关闭 |
| 8 | `Root Cause` | 仅在假设收敛后写 |
| 9 | `DoD` | 每条可观测、可验证 |
| 10 | `Next Step` | 仅在 Root Cause 写完后写 |

**关键硬约束**：`Root Cause` 字段为空时，`Next Step` 只允许写"补证据 / 召集决策"，禁止任何"改 X / 默认 Y"。

### 类型 B · Improvement（现状可用但需演进）

`Current Behavior` → `Limitation` → `Trigger Condition` → `Options Considered` → `DoD` → `Next Step`

`Trigger Condition` 未达成 → 状态保持 `triaging` / `blocked`，不进 `planned`。

### 类型 C · Validation（已实现待验证）

`Subject` → `Test Plan` → `Pass Criteria` → `Environment` → `Result`（全 pass → 移出文件）

完整字段定义与边界条件见 `references/checklist.md`；完整反例 → 正例见 `references/examples.md`。

## 文件结构、ID 与优先级

文件骨架：

```text
# Known Issues & Backlog
最后更新: YYYY-MM-DD

[Active Issues 索引表：ID | type | 问题 | 优先级 | 状态 | 当前阶段下一步]

[每条 Issue 用 H2 标题，前三行声明 type/status/priority，
再按字段契约顺序展开]
```

索引表的「当前阶段下一步」**只描述本阶段动作**（如「补复现样本」），不出现最终方案。

| 模块 | 前缀 | | 优先级 | 触发条件 |
|---|---|---|---|---|
| API / 主流程 | `API-` | | P0 | 功能完全不可用 / 数据丢失 / 演示阻塞 |
| Ingest / 上传 / 解析 | `ING-` | | P1 | 功能降级 / 可绕过 / 信任受损 |
| Report / Evidence / Scorecard | `REP-` `EVD-` | | P1 | 结论不可验证 / 报告误导 |
| Chat / Rewrite / Feedback | `CHAT-` `REW-` `FB-` | | P2 | 体验问题但可用 |
| Frontend / Demo UI | `FE-` | | P2 | 演示效率下降 |
| Evaluation / 部署 / 运行时 | `EVAL-` `OPS-` | | P3 | 长期改进 / 触发条件未达成 |

**ID 永不复用**（外部链接稳定性）；优先级不是工作量（"容易做" ≠ P0）。

## 自检（提交前）

- [ ] 每条 Issue 声明 `type` / `status` / `priority`
- [ ] 状态在 4 合法值内
- [ ] Bug 类 `Root Cause` 未填时 `Next Step` 只写"补证据 / 决策"
- [ ] 至少一条可原样执行的 `Repro`
- [ ] 引用日志/payload 原文作 `Evidence`
- [ ] 索引表「下一步」只写当前阶段动作
- [ ] 无已关闭 / 已上线残留
- [ ] 单条 < 60 行

完整 checklist：`references/checklist.md`

## 链路

- 设计依据与业界对照：`references/design-rationale.md`
- 反例 → 正例完整对照：`references/examples.md`
- 单次事件复盘：git commit / `docs/private/`
- 收敛后不变量：`writing-pitfall-archive`
- 跨项目沉淀：`writing-engineering-playbook`
- 长期决策：`writing-architecture-docs`
