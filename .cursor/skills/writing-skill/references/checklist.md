# Skill Quality Checklist

提交前必须逐条过。命中任意 ❌ 必须修；命中 ⚠️ 视情况判定。

## L1 · Frontmatter

- [ ] `name` 字段存在，1-64 字符
- [ ] `name` 仅含 `a-z 0-9 -`，不首尾 `-`、不连续 `--`
- [ ] `name` 严格等于父目录名（agentskills.io spec 硬约束）
- [ ] `description` 字段存在，1-1024 字符
- [ ] `description` 是**单行 YAML scalar**（不是 `>-` / `|` 多行）
- [ ] `description` 包含 **what**（做什么）
- [ ] `description` 包含 **when**（触发场景 / 用户措辞）
- [ ] `description` 至少 3 个触发关键词（中 / 英 / 同义词）
- [ ] `description` 在关键模糊场景下含 **negative trigger**（"Do NOT use for ..."）
- [ ] `description` 字符数 <300（关键词稀释临界）
- [ ] 仅在确实有平台 / 工具 / 依赖要求时才写 `compatibility`

## L2 · Body 体量

- [ ] 总行数 <500（Anthropic 硬建议）
- [ ] 总行数 <150（最优区间）
- [ ] 顶级 sections ≤8（≥9 必须合并或下沉）
- [ ] 任一段落 <100 词（≥100 拆 bullets）
- [ ] 代码块 ≤3（≥4 移 `references/examples.md`）
- [ ] `description` + body 累计触发词覆盖所有典型场景

## L2 · Body 写作风格

- [ ] 主要使用祈使句（`Read X.` 而非 `You should read X.`）
- [ ] 重 MUST / ALWAYS / NEVER 计数 <6（否则改为 reasoning）
- [ ] 每条硬约束附带 why（解释比规则有效）
- [ ] 至少 1 组反例 → 正例对照
- [ ] 没有"对话痕迹 / 决策辩证 / TBD / 待定 / 后续会处理"占位语言
- [ ] 没有平台硬假设（除非 `compatibility` 显式声明）

## L3 · References / Scripts / Assets

- [ ] `SKILL.md` 内 references 链接全部可达（无 broken link）
- [ ] 单 reference 文件 <300 行；>300 行需自带 TOC
- [ ] 没有 orphan reference（声明但 `SKILL.md` 未引用）
- [ ] 嵌套 ≤2 层（避免 `references/foo/bar/baz.md`）
- [ ] scripts 文件具备 shebang（`#!/usr/bin/env python3` 等）
- [ ] scripts 自带 usage 注释或独立 README

## 跨 skill 一致性（项目级，可选）

- [ ] 与同项目其它 skill 的命名前缀一致（如 `writing-*` 系列）
- [ ] 与同项目其它 skill 的 section 结构相近（降低读者切换成本）
- [ ] description 中提及的目标文件 / 路径与项目实际一致

## 可触发性回归

提交后做 3 个真实用户 prompt 测试（混合正向 + 边界 + 负向）：

- [ ] 正向 prompt 1（自然措辞）：能触发？
- [ ] 正向 prompt 2（含同义词 / 缩写）：能触发？
- [ ] 负向 prompt（near-miss，应不触发）：未误触发？

任一失败 → 回去调 `description` 关键词或 negative trigger。
