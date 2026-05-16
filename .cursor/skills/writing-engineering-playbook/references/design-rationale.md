# Playbook Design Rationale

## 为什么主轴是 Agent / LLM，不是通用后端

通用 SRE / 后端工程的 playbook 已经汗牛充栋（Google SRE Workbook、Designing Data-Intensive Applications）。再写一本意义不大；价值低。

Agent / LLM 应用工程是新领域，工程直觉还在沉淀中，少有人系统总结。把 ScriptLens / Agent Web App 这类项目踩过的坑抽象为可复用直觉，是有差异化价值的——既能服务自己未来项目，也能投技术博客 / 飞书获得反馈。

## 为什么单篇 ≤ 150 行

playbook 的发布渠道是飞书 / 个人博客 / 技术作品集，不是 GitHub README。这些渠道的读者：
- 时间预算：3-5 分钟读完一篇
- 一屏可读：飞书移动端、博客阅读视图

150 行约等于 3 分钟阅读 + 一屏可滚动。超出 → 拆篇，单篇单主题更易传播。

## 为什么禁止项目业务名词

playbook 的灵魂是"跨项目复用"。出现 `SCRIPT_*` env 或 `pypdf` 等项目实现名词，会让读者立刻判定"这是 ScriptLens 的事，跟我无关"，价值归零。

抽象替换测试（见 `examples.md`）是判别工具：替换后文章仍成立 → 合格；散架 → 这是 pitfall 不是 playbook。

## 为什么单向引用（pitfall → playbook，反之禁止）

- pitfall 项目内，路径具体（`/opt/apps/scriptlens` / `docker-compose.yml`），生命周期数月
- playbook 跨项目，路径抽象，生命周期数年

playbook 反向链 pitfall = 把 playbook 绑定到具体项目 = 破坏可移植性。

pitfall 引 playbook 是合理的：单项目沉淀想"未来跨项目复用"，提示自己将来抽象。

## 为什么用伪代码而非真实语言

伪代码（`text` 块）跨技术栈复用。一旦用 Python / TypeScript / Go 写出，读者会下意识地认为"这是给那种语言的工程师看的"，限制传播面。

Martin Kleppmann 在《Designing Data-Intensive Applications》中通篇使用伪代码 + 序列图，这就是原因。

## 为什么禁止时效语

"最近 / 当前流行 / 在 2026 年 / 最新版 X"会让文章在 1-2 年内自然过期。playbook 目标是"长青文章"——5 年后读者仍能受益。

抽象原则（如"决策与执行强一致"）的有效期远长于具体技术（如"GPT-4 是当下最强")。

## 业界对照

| 来源 | 关键概念 | 体现 |
|---|---|---|
| Stripe / Cloudflare engineering blog | 一篇一主题；表格密度高；无形容词；可执行 | §写作微观规范、§必要章节 |
| [Google SRE workbook](https://sre.google/workbook/table-of-contents/) "philosophies" 章节 | 把工程直觉沉淀为不绑定具体故障的"哲学" | §一句话准则、§硬性禁止「项目业务名词」 |
| Martin Kleppmann 《Designing Data-Intensive Applications》 | 用语言无关伪代码 + 抽象维度论证 | §设计骨架「语言无关伪代码」 |
| ADR (Architecture Decision Records) 文化 | 一份 record 只承载一个决策 | §必要章节 4 节式（现状/对比/原理/信号） |
| [Awesome Engineering Blogs](https://github.com/kilimchoi/engineering-blogs) | 长青文章不写时效语 | §硬性禁止「时效语言」 |

## 与 pitfall 的关键差异（再次强调）

pitfall 回答"我们项目今天怎么不死"；playbook 回答"任何项目长期下去都该怎么做"。两者绑定与否、时效长短、可发布性都不同。
