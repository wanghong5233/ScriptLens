# Design Rationale · 为什么这些规则

每条规则都对应一条业界验证的失败模式或第一性原理。

## 规则 1：description 必须单行

**根因**：Claude Code / 部分 Cursor 客户端的 YAML 解析对 `>-` `|` 多行 scalar 处理不一致，会让 skill 在 listing 中显示不全或完全消失。

**证据**：
- [spences10/claude-skills-cli validator](https://github.com/spences10/claude-skills-cli) 把多行描述列为强警告，doctor 命令会自动 reflow
- Anthropic skill-creator 自己的 description 字段是单行

**反推**：宁可单行长一些（300 字符内），也不要为可读性换多行。

## 规则 2：description 要 pushy

**根因**：模型在 listing 中看到 1000+ skill 时，触发是相对决策；含蓄描述会被更显眼的同类 skill 抢走。

**证据**：Anthropic Best Practices 原话：
> "Currently Claude has a tendency to 'undertrigger' skills — to not use them when they'd be useful. To combat this, please make the skill descriptions a little bit 'pushy'."

**反推**：列触发关键词不是"hack"，是 spec 推荐写法。

## 规则 3：body <500 行

**根因**：skill 触发后 body 一次性进 context 并停留整段对话，每行都是复发成本。

**证据**：
- [agentskills.io spec](https://agentskills.io/specification#progressive-disclosure)："Keep your main SKILL.md under 500 lines."
- Anthropic 内部 best practice："<5000 tokens recommended"
- 第三方 validator 把 <50 行设为"excellent"、150 行为警告线

**反推**：500 是上限不是目标。"Many effective skills are 20-50 lines"（Anthropic 原话）。

## 规则 4：解释 why，少用 MUST/NEVER

**根因**：LLM 的 theory of mind 强，理解原因后能处理 spec 中没列的边界；堆规则会让模型在新场景僵化。

**证据**：Anthropic Best Practices 原话：
> "If you find yourself writing ALWAYS or NEVER in all caps, or using super rigid structures, that's a yellow flag — if possible, reframe and explain the reasoning so that the model understands why the thing you're asking for is important."

**反推**：写 skill 不是写 RFC；写出"模型能复用的直觉"，不是穷举边界。

## 规则 5：拆分到 references/

**根因**：Progressive Disclosure 三层架构本质是 token 成本分级——L3 按需加载、不计入 always-on 预算。

**证据**：
- agentskills.io 4-stage loading model
- DeepWiki "1000+ skills 全量 vs metadata-only 加载差异：96.3% token 节省、93% latency 改善"

**反推**：references/ 不是文档归档，是 token 经济学优化。

## 规则 6：name 必须匹配目录

**根因**：发现机制（Cursor / Claude Code）通过目录名定位 skill，name 字段不一致会导致引用断裂。

**证据**：[agentskills.io spec](https://agentskills.io/specification#name-field)："Must match the parent directory name."

## 规则 7：测试 3 个真实 prompt

**根因**：用户措辞 ≠ 开发者措辞。开发者常用的术语用户未必说，反之亦然。

**证据**：Anthropic skill-creator 原话：
> "The queries must be realistic and something a Claude Code or Claude.ai user would actually type. Not abstract requests, but requests that are concrete and specific... Some might be in lowercase or contain abbreviations or typos or casual speech."

**反推**：写完 skill 自己跑 3 次远比脑补"应该会触发"靠谱。

## 规则 8：与 Cursor Rule / CLAUDE.md 分清边界

**根因**：三者加载机制不同——

| 机制 | 何时加载 | 成本模式 |
|---|---|---|
| Cursor Rule / CLAUDE.md | 永远在 context | 全程付费 |
| Agent Skill description | 永远在 listing | 微量 |
| Agent Skill body | 触发时全量 | 整段对话付费 |
| MCP tool | 调用时 | 单次调用 |

**误用代价**：把永久约束写进 skill → 有时丢失；把任务流程写进 Rule → 全程冗余成本。

## 规则 9：单 reference 文件 <300 行 / 自带 TOC

**根因**：reference 加载时整篇进 context，太长会重蹈 body 过载的覆辙。

**证据**：Anthropic Best Practices 原话：
> "For large reference files (>300 lines), include a table of contents."

## 信息来源汇总

| 来源 | 用途 |
|---|---|
| [agentskills.io spec](https://agentskills.io/specification) | 跨平台硬约束 |
| [Anthropic Best Practices](https://www.mintlify.com/anthropics/skills/creating-skills/best-practices) | 写作准则 |
| [anthropics/skills/skill-creator](https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md) | 参考实现 |
| [Claude Code skills docs](https://code.claude.com/docs/en/skills) | Claude Code 特有扩展 |
| [Cursor skills docs](https://cursor.com/docs/skills) | Cursor 特有扩展 |
| [spences10/claude-skills-cli](https://github.com/spences10/claude-skills-cli) | 量化阈值参考 |
