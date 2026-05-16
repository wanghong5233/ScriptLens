# README Design Rationale

## 为什么是"项目卡片"，不是"实现手册"

GitHub README 是项目对外的第一接触点。访客只有 10 秒决定是否继续看（GitHub 10-second rule）：
- 这是什么？
- 我为什么要关心？
- 怎么试一下？

超出 10 秒还没答上 → 失败。任何"实现细节 / 内部决策 / 部署细节"都偏离这个目的，应迁到 `docs/`。

## 为什么 ≥100 行需要 TOC

standard-readme spec 明确规定。读者在长 README 里没有导航 = 强迫线性扫描 = 违反 10 秒规则。

## 为什么禁止形容词自夸

"强大 / 优雅 / 业界领先 / 创新"这类形容词：
- 无信息量：读者无法验证
- 贬值快：所有项目都用 → 边际效用为 0
- 损害专业感：Stripe / Vercel / Anthropic 公司项目 README 几乎不用

事实陈述优于形容修饰。

## 为什么子 README 是根 README 的补集

如果子 README 复述根 README 内容：
- 维护成本翻倍（两处同步）
- 读者迷失（不知道哪个是主入口）
- 漂移风险（两处内容逐渐不一致）

补集原则消除冗余：根 README 说项目是什么；子 README 说子目录内独有的开发/运维细节。

## 为什么必须双语严格对齐

`README.md`（中文）+ `README_EN.md`（英文）面向不同读者群。但任一漂移都伤害：
- 中文读者看英文版无法验证项目对中国市场严肃性
- 英文读者看中文版无法验证项目对国际访问严肃性

→ 同步修改是硬约束。

## 业界对照

| 来源 | 关键约束 | 体现 |
|---|---|---|
| [standard-readme spec](https://github.com/RichardLitt/standard-readme/blob/main/spec.md)（RichardLitt） | Title / Short Description / Contributing / License 必备；章节顺序固定；>100 行需 TOC | §标准章节顺序、§硬性禁止「失效链接」 |
| GitHub README 10-second rule | 读者 10 秒内能答 What / Why / How try it | §一句话准则、§标准章节顺序前 4 节 |
| [awesome-readme](https://github.com/matiassingers/awesome-readme) hall-of-fame | Logo + Tagline + Badges + Demo GIF + 一句话 pitch + 三步 Quick Start | §标准章节顺序 1-8 |
| Stripe / Vercel / Anthropic 公司项目 README | 无形容词自夸、表格密度高、外链点验严格 | §写作微观规范、§硬性禁止「自我夸赞形容词」 |

## 触发更新的判别原则

"变化是否影响外部读者对项目的第一印象 / 决定是否上手"。是 → 改 README；否 → 不改。

| Git 变化 | 是否更新 |
|---|---|
| 服务增删（compose service 变化） | 必改：Architecture / Tech Stack |
| 核心依赖切换（ES → pgvector） | 必改：Architecture / Background |
| 外部 URL 改变（demo / docs） | 必改：Header / Demo / Documentation |
| 内部实现优化（同职责换库） | 不改 → `docs/CHANGELOG` |
| 部署细节调整 | 不改 → `docs/` 部署手册 |
| Bug 修复 | 不改 → git commit |
| 单纯重命名变量 | 不改 → git log |
