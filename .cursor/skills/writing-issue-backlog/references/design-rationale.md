# Issue Backlog Design Rationale

## 为什么字段顺序硬约束

LLM 与人类在面对"问题"时都有强烈的"跳到解法"倾向（confirmation bias 的近亲）。Cynefin Framework 将问题分类为 Simple / Complicated / Complex / Chaotic 四象限——Complicated 域必须先 Sense → Analyze → Respond，跳序会产生"看起来合理但根因错位"的方案。

字段顺序 `Symptom → Repro → Evidence → Hypotheses → Root Cause → DoD → Next Step` 把"跳序"做成结构性不可能：当读者/作者翻到 Next Step 时，前面所有字段都已强制填写。

## 为什么状态机只有 4 个值

业界常见 issue tracker（GitHub / Jira / Linear）状态有 8-15 个，混合了"问题阶段"与"实现阶段"。这种混合让"问题是否定义清楚"和"是否开始动工"无法区分——常见后果：`in_progress` 的 issue 实际上 Root Cause 还没找到。

本文件只关心"未解决的活跃问题"，因此只需 4 个值：
- `triaging`（问题未定义清楚）
- `investigating`（已定义，根因未定）
- `planned`（根因已定，待实现）
- `blocked`（外部阻塞）

实现状态由 git PR / commit 自然承载，不在本文件重复维护。

## 为什么三类 type 分流

把 Bug、Improvement、Validation 套同一模板会产生灾难：
- Bug 套 Improvement 模板 → 没有 Repro，无法验证
- Improvement 套 Bug 模板 → 没有 Trigger Condition，永远在"可改可不改"灰区
- Validation 套 Bug 模板 → 没有 Test Plan，验证完不知道是否通过

分流的代价是 Schema 复杂，收益是字段对每类问题都有意义。

## 为什么关闭后直接删除

GitHub Issues / Jira 等都默认保留已关闭 issue。但本文件是 markdown，读者每次查阅都全文扫描——保留已关闭条目会让"当前焦点"被噪音稀释。

git 历史提供了"考古"能力，足以应对"这个 bug 当初是怎么定的"的需求。删除是免费的，保留有成本。

## 为什么 ID 永不复用

外部链接（commit message / PR description / Slack 引用）使用 ID 作为锚点。复用号段会让历史链接指向语义错误的内容（同一 ID 在不同时间指代不同问题）。

代价：ID 计数器无限增长。这是可以接受的——ID 是 string 不是数据库主键。

## 业界对照

| 来源 | 关键概念 | 体现 |
|---|---|---|
| [Mozilla Bug Writing Guidelines](https://bugzilla.mozilla.org/page.cgi?id=bug-writing.html) | "Be precise. Be clear. Show, don't tell."；Steps-to-reproduce、Expected vs Actual 必填 | 类型 A 必填 Symptom / Repro / Observed Evidence |
| Chromium triage process | `needs-repro` 状态、状态机收敛、ID 不复用 | 4 值状态机 + ID 永不复用 |
| Google SRE Blameless Postmortem | Detection / Timeline / Root Cause / Action Items 分离 | Bug 字段集与五段对齐，但本文件是"未关闭"形态 |
| Cynefin Framework（Snowden） | Complicated 域必须先 Sense → Analyze → Respond | 字段顺序硬约束 |
| Toyota Production System / 5 Whys | "未找到根因前，禁止开 Action Item" | Root Cause 未填时 Next Step 只能"补证据" |
| GitHub Issue Templates（OSS 通用） | 多 type 分流模板 | 类型 A/B/C 字段集分流 |
| Linear / Shortcut Idea→Spec | Improvement 类先写 Trigger Condition 再写 Solution | 类型 B 必填 Trigger Condition |
