---
name: writing-engineering-playbook
description: Write or refactor cross-project engineering playbook (Agent/LLM + backend; source in `docs/private/engineering-playbook/`, publish to 飞书/blog NOT GitHub). Use when user asks to 写/沉淀/提炼 工程经验/Agent 经验/playbook/第一性原理/跨项目复用/技术博客. Do NOT use for project pitfalls.
---

# Writing Engineering Playbook

## 一句话准则

**一篇 playbook 只回答一件事：跨项目复用的工程直觉是什么、为什么是它。绑定项目的内容、辩证过程、对话痕迹都不是 playbook。**

## 与 pitfall / architecture / readme 的边界

| 文档 | 项目绑定 | 时效 | 发布渠道 | 形态 |
|---|---|---|---|---|
| **Playbook** | **无** | 几年 | **飞书 / 博客** | 反模式 vs 正例 + 第一性原理 + 信号 + 自检 |
| Pitfall Archive | 强（路径/容器/env） | 几个月 | 仓库 docs/ | 五段式（Symptom/Evidence/RC/Solution/Invariant） |
| Architecture Doc | 强 | 年级别 | 仓库 docs/ | 现状 + 第一性原理表 + 契约 |
| README | 强 | 持续 | GitHub 主页 | standard-readme 章节 |

**判别口诀**：把项目业务名词（`pgvector` / `doc_studio` / `SM_*` env / `LlamaParse`）改成抽象概念（向量索引 / 协作服务 / 业务参数 / 文档解析），文章仍成立 → 合格；立刻散架 → 这是 pitfall 或 architecture，不是 playbook。

## 主轴 / 副轴（决定写什么主题）

| 轴 | 范围 | 示例主题 |
|---|---|---|
| **主轴 · Agent / LLM** | LLM 应用 / Agent 编排 / RAG / 工具调用专属 | Provider 级熔断；Prompt 是契约；工具调用后一致性；评估闭环；上下文预算；记忆分层；意图路由的退路 |
| **副轴 · 通用后端 / 分布式** | 任何工程都适用 | 参数治理；决策-执行强一致；失败要响亮；可观测即合同；灰度与回滚；启动期校验 |

**主轴必须超过副轴**：一篇 Agent + 一篇通用，再写第二篇 Agent。保持 playbook 的差异化身份（不是又一本 SRE 手册）。

## 硬性禁止（命中即删）

| 反模式 | 判断特征 | 归宿 |
|---|---|---|
| 项目业务名词 | `scriptlens_*` / `SCRIPT_*` / `pypdf` / `EvidenceRef` / `DashScope` | 改抽象；改不掉 → 改投 pitfall |
| 散文化讲故事 | "我们曾经……后来……于是……" | 改第一性原理维度表 |
| 决策辩证过程 | "考虑过 A / B / C，最后选 D" | git log / `private/` |
| 业界对比铺垫 | "Stripe 这样做，OpenAI 那样做" | 删；至多 1 句引用 |
| 喊口号 | "工程师要有责任感 / 系统要健壮" | 删；改可验证检测信号 |
| 一篇多原理 | 标题"分布式系统设计精要" | 拆单篇单主题 |
| 教程化 | "首先 / 第二步 / 第三步" | 改 mermaid 或伪代码骨架 |
| 引用未公开内部链接 | 链 `private/` 或公司内 wiki | 删；外链必须可公开访问 |
| 时效语言 | "在 2026 年 / 最新版 X / 当前流行" | 改无时效表述 |

设计依据见 `references/design-rationale.md`。

## 必要章节（按 01/04 实际形态归纳）

每节缺哪一块不强求，**出现即必须是这种形态**：

1. **现状陈述**（一段，现在时）：直接说"现在的规则是什么"，不写背景、不写"过去怎么做"
2. **反模式 vs 正例**（表格）：≥5 行，每行一个观察维度，无解释段落
3. **第一性原理**（维度表，5 行内）：`维度 / 分析 / 结论`；维度名选抽象语义（调用面 / 异质性 / 可逆性 / 故障域 / 可观测 / 数据正确性 / 信噪比 / 责任归属 / 复发频率）
4. **触发抽象的信号**（编号列表 4-7 条）：可观测、可验证的检测特征，不写直觉
5. **设计骨架 / 检测信号 / 适用边界**（按主题选用）：伪代码用 `text` 块（**语言无关**），不写 Python/Go 实现
6. **自检清单**（5-7 条复选框）：每条可单点验证
7. **反向链接**（playbook 内部交叉引用）：形成网状结构

**单向引用规则**：pitfall 可引 playbook，**playbook 不引 pitfall**（破坏可移植性）。

完整反例 → 正例对照见 `references/examples.md`。

## 写作微观规范

- 标题：`NN · 中文名 / English Name`（双语，与 01/04 对齐）
- 中文正文 + 英文代码 / 伪代码标识符
- 现在时陈述（不写"我们曾经 / 后来 / 这次"）
- 段落 ≤ 3 行；超过改表格 / mermaid / 伪代码
- 单篇 ≤ 150 行（飞书一屏可读）；超过拆篇
- 表格密度高于文字密度
- 无 emoji、无感叹号、无形容词自夸
- 不写时效语

## 自检（提交前必过）

### 内容质量
- [ ] 把所有项目业务名词改抽象词，文章仍成立？不成立 → 删 / 改投 pitfall
- [ ] 出现"我们 / 我 / 曾经 / 后来 / 这次"？→ 改现在时
- [ ] 反模式 vs 正例表 ≥ 5 行？
- [ ] 第一性原理表维度名是抽象语义（异质性 / 可逆性 / 故障域），还是项目术语？
- [ ] 触发信号是可单点验证的特征，还是直觉？

### 开源就绪
- [ ] 含真实 IP / hostname / API key / 内部域名？→ 删
- [ ] 含未公开仓库链接（公司内 wiki / `private/`）？→ 删
- [ ] 另一个项目（电商 / 社交）的工程师读起来仍有指导价值？无 → 改投 pitfall
- [ ] 标题与文件名编号一致？反向链接全部跳得通？

## 链路

- 设计依据：`references/design-rationale.md`
- 反例 → 正例：`references/examples.md`
- 项目内坑点档案：`writing-pitfall-archive`
- 项目架构文档：`writing-architecture-docs`
- 项目对外名片：`writing-readme`
- 当前 playbook 实例集：`docs/private/engineering-playbook/`（已有 01-07）
