# Pitfall Archive Design Rationale

## 为什么不是 Postmortem，也不是 Runbook

| 文档类型 | 范围 | 时态 | 位置 |
|---|---|---|---|
| Postmortem（单次复盘） | 单一事件 | 过去式：发生过什么 | git commit / `private/` |
| Runbook（操作手册） | 高频流程 | 命令式：怎么做 | `backend/scripts/` 的脚本即是 |
| **Pitfall Archive**（坑点档案） | 多次故障沉淀 | 现在时：现在的规则是什么 | `docs/*MANUAL*.md` |

**关键差异**：
- Postmortem 是事件级临时产物，可归档
- Pitfall Archive 是"项目级不变量库"，永远活的、持续演进

把单次事件全文写进 pitfall = 把事件叙事和规则沉淀混淆，读者下次故障时无法快速找规则。

## 为什么不写命令序列

命令序列是 Runbook 的职责。pitfall 写命令会出现两个问题：
1. 命令会过期（路径、版本、container 名字变化），但 pitfall 是长生命周期文档
2. 命令在 pitfall 里散落，无法版本控制、无法自动化

正确做法：命令抽成 shell 脚本（`backend/scripts/`），pitfall 引用脚本路径。

## 为什么编号永不变

外部脚本 / 文档 / commit message 可能 `grep §4.3` 作为锚点引用。编号变化会让历史链接指向语义错误的内容。

代价：废弃条目用 `~~strikethrough~~` 标记保留一个版本再清理。

## 为什么严格单向引用（pitfall → playbook，反之禁止）

- pitfall 项目内，路径具体，生命周期数月
- playbook 跨项目，路径抽象，生命周期数年

playbook 反向链 pitfall = 把 playbook 绑定到具体项目 = 破坏可移植性。

pitfall 引 playbook 合理：单项目沉淀想"未来跨项目复用"，提示自己将来抽象。

## 业界对照

| 来源 | 关键概念 | 体现 |
|---|---|---|
| [Google SRE Postmortem](https://sre.google/workbook/postmortem-culture)（blameless） | Detection / Root Cause / Action Items / Lessons Learned；对事不对人；事实先于解读 | §4 五段式（Symptom = Detection, Evidence = Timeline, Root Cause = RCA, Solution = Action Items, Invariant = Lessons Learned） |
| [Google Cloud Conduct Postmortems](https://docs.cloud.google.com/architecture/framework/reliability/conduct-postmortems) | Postmortem 是活文档，要可搜索、可链接、可回溯 | §7 演进规则（编号永不变 / 不变量升级流程） |
| Atlassian PIR (Post-Incident Review) | 关注系统而非个人，关注预防而非追责 | §硬性禁止「单次事件的完整记录」「决策辩证过程」 |
| AWS Well-Architected Operational Excellence | "Learn from operational failures" 但不沉湎事件叙事 | Pitfall Archive vs Postmortem 的角色边界 |

## 与单次 Postmortem 的关键差异

- Postmortem 写"发生过什么"
- Pitfall Archive 写"现在的规则是什么"

两者互补：事件复盘进 git commit / `private/`，沉淀下来的规则进 `docs/` 档案。
