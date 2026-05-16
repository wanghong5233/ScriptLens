---
name: writing-skill
description: Write or refactor an Agent Skill / SKILL.md (agentskills.io spec; portable across Cursor, Claude Code, etc.). Use when user asks to 写/create/refactor/audit/优化 a skill or SKILL.md, or asks why a skill 没触发/太长. Do NOT use for Cursor Rules, CLAUDE.md, AGENTS.md, MCP server.
---

# Writing Agent Skills

## 一句话准则

**一个 skill 只回答两件事：做什么 (what) + 什么时候用 (when)。其余一切能搬进 `references/` 就别留在 SKILL.md，因为 body 每一行都是会话级复发 token 成本。**

## 第一性原理：Progressive Disclosure

| 层 | 加载时机 | 预算 | 装什么 |
|---|---|---|---|
| L1 metadata (YAML) | 永远在 context | ~100 tokens | `name` + `description` |
| L2 SKILL.md body | 触发时全量加载 | **<5000 tokens / <500 行** | 流程主干 + 关键约束 + 1-2 例子 + 指针 |
| L3 `references/` `scripts/` `assets/` | 按需加载 | 不限 | 长篇参考、可复用脚本、模板 |

体量越过 L2 预算 = 错位（应下沉 L3）。Anthropic 原话：*"Once a skill loads, its content stays in context across turns, so every line is a recurring token cost."*

## Frontmatter 与 description

```yaml
---
name: skill-name
description: <single line; 100-300 chars; what + when + trigger keywords + (optional) negative triggers>
---
```

硬约束（agentskills.io spec）：

- `name`：1-64 字符，仅 `a-z 0-9 -`，不首尾 `-`、不连续 `--`，**必须等于父目录名**
- `description`：1-1024 字符；**单行 YAML**（多行会让 Claude Code 等客户端识别失败）；甜区 <300

`description` 反直觉的写法——模型倾向 under-trigger，含蓄描述等于沉默。要适度 **pushy**：

| 维度 | 反例 | 正例 |
|---|---|---|
| What | "Helps with documents" | "Extract PDF text, fill PDF forms, merge PDFs" |
| When | 留空 | "Use when user mentions PDF / .pdf / 表单填充" |
| 触发词 | 含蓄 | 显式列同义词、缩写、中英文 |
| Negative | 无 | "Do NOT use for Word / Excel / Markdown" |

完整对照与触发词组合策略：`references/examples.md`

## Body 写作五条 + 体量信号灯

1. **解释 why，不只 what**：模型懂原因后会泛化；堆 ALWAYS/NEVER/MUST 是 yellow flag（Anthropic 明确警告）
2. **祈使句**：`Read the input.` ✅ vs `You should read the input.` ❌
3. **具体例子 > 抽象描述**：1 个 input→output 反例+正例胜过 5 段说明
4. **每删一行问"它在拉动结果吗"**：不能就删；body 越短，模型越聚焦
5. **多变体场景上 L3**：SKILL.md 写选择逻辑，每变体细节进 `references/<variant>.md`

体量信号灯（提交前自审）：

| 维度 | 优 | 良 | 警告 → 行动 |
|---|---|---|---|
| body 行数 | <50 | <150 | ≥150 → 拆 `references/` |
| 顶级 sections | 3-5 | 6-8 | ≥9 → 合并或下沉 |
| 代码块 | 1-2 | 3 | ≥4 → 移 `examples.md` |
| 单段落字数 | <60 | <100 | ≥100 → 拆 bullets |
| description 字符 | <200 | <300 | ≥300 → 关键词稀释 |
| 重 MUST 计数 | <3 | <6 | ≥6 → 改 explanation |

完整自检清单：`references/checklist.md`

## 标准目录结构与边界

```text
skill-name/
├── SKILL.md              # body + 指针（必填）
├── references/           # 长篇参考，按需读
├── scripts/              # 可复用脚本
└── assets/               # 模板 / 数据
```

判断"是否拆 `references/`"：同内容在 ≥3 个 skill 重复，或单段 >300 行 → 拆。

与邻近机制的边界：

| 机制 | 何时加载 | 何时用 |
|---|---|---|
| **Agent Skill** | description 永驻；body 触发时全量 | 反复出现的、有完整流程的任务 |
| Cursor Rule / AGENTS.md / CLAUDE.md | 永远在 context | 编码风格、命名约定、不变量 |
| MCP server | tool call | 需要执行外部能力 |

误把 skill 当 Rule（永久加载） 会撑爆 context；误把 Rule 当 skill（按需触发） 会让永久约束有时丢失。

## 数量治理：Skill Pack 不是越多越好

每个 skill 即使不触发，`description` 也会进入可发现能力列表，形成 **invisible context tax**。高 star 仓库适合当 upstream benchmark，不适合整包常驻。

| 作用域 | 建议上限 | 规则 |
|---|---|---|
| 项目级 `.cursor/skills/` | 5-8 | 只放项目强相关流程 |
| 个人级 `~/.cursor/skills/` / `~/.agents/skills/` | 8-12 | 放跨项目高频工程能力 |
| 总 active skill | ≤15 | 超过就复审合并/禁用 |
| marketplace / awesome list | 不常驻 | 只作为检索与借鉴来源 |

新增 skill 前先问：它能否合并进已有 skill？能否改成 `references/`？是否只是开源 skill 的低差异复制？

## 创作循环与自检

创作循环（Anthropic skill-creator 派生）：

1. **意图**：模型在什么场景做什么？预期输出？触发词哪些？
2. **草稿**：<100 行 SKILL.md，focus 流程主干
3. **2-3 真实测试 prompt**：模拟真实用户用语（含口语、缩写、错别字）
4. **复盘**：偏离在哪里？description 没触发、body 缺指引、还是体量过载？
5. **针对性修**：能搬 L3 的搬走、能换 explanation 的换、不拉动结果的删
6. **迭代直到稳定**

提交前最小自检：

- [ ] `name` 匹配目录名、kebab-case
- [ ] `description` 单行、含 what+when+触发词、<300 字符
- [ ] body <500 行、顶级 sections ≤8
- [ ] 至少 1 个具体反例 / 正例
- [ ] 重 MUST 已换为 reasoning，或确实必要
- [ ] 大块内容已下沉 `references/`

完整 checklist：`references/checklist.md`

## 业界依据

- [agentskills.io specification](https://agentskills.io/specification)：跨平台开放标准
- [Anthropic Best Practices](https://www.mintlify.com/anthropics/skills/creating-skills/best-practices)：写作准则原文
- [anthropics/skills/skill-creator](https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md)：参考实现
- 详细设计依据与引文：`references/design-rationale.md`
