---
name: writing-readme
description: Write or refactor public README per standard-readme spec + GitHub 10-second rule. Use when editing `README.md`/`README_EN.md`/subfolder README, or user asks to 写/同步/修改/更新 README, or complains README has 口水话/啰嗦/内部业务字段/不像专业 GitHub 项目. Do NOT use for `docs/*.md` or internal docs.
---

# Writing READMEs

## 一句话准则

**README 是项目卡片，不是实现手册。访客用 10 秒决定是否继续看；超出 10 秒还没回答"这是什么 / 我为什么要关心 / 怎么试一下"就是失败。**

## 角色边界（决定写什么 / 不写什么）

| 文档 | 受众 | 内容粒度 | 链接去向 |
|---|---|---|---|
| 根 `README.md` | 公开访客、招聘方、用户 | 项目愿景 / 架构总览 / 三模块概述 / demo 入口 | demo、`docs/`、外部主页 |
| 子目录 `<sub>/README.md` | 该子目录开发者 / 运维 | **根 README 的补集**：仅含子目录独有内容 | 链回根 README + `docs/` |
| `docs/*.md` | 严肃读者、内部协作者 | 部署、坑点、设计 | 架构文档 |
| `docs/private/*.md` | 仅本人 | 笔记、面经、archived 草稿 | 不公开 |

**子 README 补集原则**——判别口诀：删掉这段后，根 README 仍能让人启动起来吗？能 → 子 README 该写它；不能 → 它属于根 README。完整对照见 `references/examples.md`。

**README 不承担**：部署细节、故障排查全集、内部 env 对齐表、SQL/Python 函数名、未发布的实验功能。

## 硬性禁止（命中即删）

| 反模式 | 判断特征 | 归宿 |
|---|---|---|
| 口水话 / 解释性铺垫 | "为了 / 这是因为 / 我们采用 / 值得一提的是" | 删 |
| 内部业务字段 | `ts_rank_cd` / `SM_RETRIEVE_PAGE_SIZE` / pydantic schema 名 | `docs/` |
| 未启用功能的辩解 | "生产默认不启用 / 仅开发期使用" 写进高层描述 | 删 |
| 过程时态 | "曾经使用过 X / 后来切到 Y / 这次重构了 Z" | git log / CHANGELOG |
| 内部链接 | 指向 `private/` 或未 tracked 路径 | 改指 `docs/` 或外部 URL |
| 重复架构图 | 同一架构画 ASCII + mermaid + 文字 3 份 | 留一份（mermaid 优先） |
| 失效 demo 链接 | demo 跳 404 或指已废弃子域 | 改新链接，**提交前必须人工点击** |
| 工件清单 | "本次移除了 elasticsearch / reranker" | git log |
| 自我夸赞形容词 | "强大的 / 优雅的 / 业界领先的 / 创新的" | 删 |

设计依据见 `references/design-rationale.md`。

## 标准章节顺序（standard-readme spec 对齐）

```text
1. Title              ← 与仓库名一致
2. One-line Tagline   ← <120 字符；package.json / setup.py description 同源
3. Badges             ← 3-5 个：CI / License / Stars / Demo / Tech
4. Demo               ← GIF/截图/Live link，二者其一必备
5. Background         ← 一段话：解决什么问题，给谁用
6. Features           ← 5-8 个 bullet，可扫描
7. Architecture       ← mermaid 图 + 分层职责表（一图代千言）
8. Quick Start        ← 3 步以内能跑起来；命令必须真实可复制
9. Tech Stack         ← 表格：前端 / 后端 / 数据 / 基础设施
10. Documentation     ← 链接到 docs/ 子文档
11. Contributing      ← 必备（即使只是一行）
12. License           ← 必备
```

**100 行以上的 README 必须有 TOC**（standard-readme 硬要求）。

## 触发更新的时机

| Git 变化 | 是否更新 | 改哪 |
|---|---|---|
| 服务增删（compose service 变化） | 必改 | Architecture / Tech Stack |
| 核心依赖切换（ES → pgvector） | 必改 | Architecture / Background |
| 外部 URL 改变（demo / docs） | 必改 | Header / Demo / Documentation |
| 内部实现优化（同职责换库） | 不改 | `docs/CHANGELOG` |
| 部署细节调整 | 不改 | `docs/` 部署手册 |
| Bug 修复 | 不改 | git commit |
| 单纯重命名变量 | 不改 | git log |

判别原则见 `references/design-rationale.md`。

## 写作微观规范

- 中文 README 用中文正文，技术名词 / 标识符保留英文（不翻译 `pgvector` 为"PG向量"）
- 现在时陈述：❌"我们决定采用 X" → ✅"采用 X"
- 段落 ≤ 3 行；超过改表格 / 列表 / mermaid
- 链接必须可点：相对路径用 `./docs/xxx.md`；外链带 `https://`；**提交前批量点验**
- Badges 来自 `shields.io`，颜色不超过 3 种
- mermaid 图节点数 ≤ 12；超过拆分图
- 双语 README：`README.md`（中文）+ `README_EN.md`（英文）；**两份内容严格对齐**，同步更新

## 自检（提交前必过）

### 通用
- [ ] 删掉这行，10 秒读者会漏什么核心事实？漏不掉 → 删
- [ ] 业务字段名 / 内部 env 名 / 内部函数名出现在公开 README？→ 删
- [ ] 相对路径都指向 git tracked 文件？（`docs/` ✅、`private/` ❌）
- [ ] 外部链接今天还能打开？（demo / docs / 主页）
- [ ] 形容词自夸（"强大 / 优雅 / 领先"）？→ 删

### 根 README 专属
- [ ] 中文版改了，英文版同步了吗？
- [ ] 架构图与当前 `docker-compose.prod.yml` 服务清单一致？

### 子 README 专属
- [ ] 这段删掉，根 README 仍能让人启动起来吗？能 → 该写它；不能 → 它属于根 README
- [ ] 是否复述了根 README 的项目愿景 / 架构图 / Features？→ 删
- [ ] 是否含子目录独有的开发命令 / 运维 SOP / 环境变量？→ 这才是子 README 本职
- [ ] 文档开头是否明确写"项目入口见根 [`README.md`](../README.md)"？

## 链路

- 设计依据与业界对照：`references/design-rationale.md`
- 反例 → 正例完整对照：`references/examples.md`
- 工程约束基线：`.cursor/rules/core-principles.mdc`
- 架构文档撰写：`writing-architecture-docs`
- 部署 / 坑点档案撰写：`writing-pitfall-archive`
- 跨项目工程经验：`writing-engineering-playbook`
