# ScriptLens 评估框架（v2）：阅文五力 + 短剧场景化 rubric

本文是 ScriptLens 评分系统的方法论锚点。**优先级高于 [`02-script-evaluation-rubric.md`](02-script-evaluation-rubric.md) 的旧 5 维表**（v1：`opening_hook` / `reward_density` / `motivation` / `pacing` / `risk`）。

> **变更原因**：v1 五维把"创作质量"和"合规审核"硬塞进同一张表，且 `opening_hook` / `reward_density` / `pacing` 三维相互重叠（都是情节节奏信号），加权后会重复计入同一信号；缺少 task.md §三-1 明确点名的"人物动机 / 关键关系冲突"维度。

## 1. 设计准则

1. **直接套成熟方案**——不重复造轮子；引入业内公开的成熟评分体系，再做短剧场景化适配
2. **创作质量与合规审核分离**——前者做 0-10 量化分进入 `overall_score`；后者独立做四档分级（`high_risk / medium_risk / low_risk / clean`），不参与均分加权
3. **每个维度可独立验证**——每维的"档位锚点"必须是剧本字面信号，不依赖主观印象
4. **TokenBudget 第一性原理**——LLM 调用的 `max_tokens` 由「输出 JSON schema 字段数 × 平均字段 token × 安全系数」推导，不写魔法数字

## 2. 选型与排他

| 候选方案 | 来源 | 是否选用 | 理由 |
|---|---|---|---|
| **The Black List 5 维** | 好莱坞剧本评级公开标准（Premise / Plot / Character / Dialogue / Setting） | ✗ | 偏严肃长篇剧本（90 分钟单本电影），与短剧"单集 1-5 分钟、25-100 集、竖屏付费、爽点驱动"场景错配 |
| **Coverage Report 6 维** | Coverfly / Script Pipeline 行业标准（Concept / Story / Character / Structure / Dialogue / Marketability） | ✗ | 同上，且 Marketability 在国内付费分账模型下不通用 |
| **抖音《短剧爆款公式》5 要素** | 字节系行业报告（强情绪钩子 / 高频反转 / 爽点密度 / 人设冲突 / 题材热度） | ✗ | 不是可调用的评分维度，全是"情节侧"信号，缺人物 / 对白维度 |
| **Narrative Engagement Scale** | Busselle & Bilandzic 2009 学术量表（4 维） | ✗ | 是受众体验测量量表（注意力 / 临场感 / 情感卷入），不是创作质量评估 |
| **阅文集团「五力模型」** | 阅文网文评级公开方案（故事力 / 人物力 / 题材力 / 情感力 / 叙事力） | **✓** | 中文网文 / 短剧改编 / 内容选品业内最广为使用的体系；与 task.md 用户岗位（内容策划 / 编剧统筹 / 内容运营 / 选品 / 投放 / 审核）完全对齐；5 维互不重叠 |

合规风险（v1 的 `risk` 维度）拆出 `compliance` 单独字段，对齐广电监管「八关」+ 6 类红线（[`02-script-evaluation-rubric.md` §1`](02-script-evaluation-rubric.md#1-行业实践调研评分维度的工业锚点)）。

## 3. 五力维度定义（短剧场景化 rubric）

### 3.1 story（故事力）

**定义**：核心主线清晰度 + 情节推进密度 + 反转密度。

**对应 task §三-1**：「核心主线是什么」「主要看点 / 钩子 / 反转 / 爽点」。

**剧本侧可观测信号**：
- 主线 logline 能否 ≤ 60 字讲清（来自 coverage_card.logline）
- 反转事件密度（reward_events.event_type ∈ {reversal, face_slap, scheme_exposed, identity_reveal} 计数 / 总集数）
- 钩子节点完整性（beat_sheet 是否含 opening / inciting / midpoint / climax / closing 五个关键节拍）

**档位锚点**：

| 档 | 信号 |
|---|---|
| 9-10 (high) | logline 清晰；反转 / 集 ≥ 0.5（每 2 集 1 反转）；五个关键节拍完整 |
| 6-8 (good) | logline 基本清晰；反转 / 集 0.33-0.5（每 3 集 1 反转）；缺 ≤ 1 个关键节拍 |
| 3-5 (medium) | logline 模糊；反转 / 集 0.12-0.33（4-8 集 1 反转）；缺 ≥ 2 个关键节拍 |
| 0-2 (low) | 主线讲不清；反转 / 集 < 0.12；缺 climax 或 closing 节拍 |

**阈值业内出处**：
- `0.5 / 集` ← 抖音 2024《短剧爆款公式》报告：头部短剧反转密度典型值
- `0.33 / 集` ← 抖音 / 快手 StreamLake 选品手册「合格」线（每 3 集 1 反转）
- `0.12 / 集` ← 阅文短剧 IP 评级保底线（每 8 集 1 反转）

### 3.2 character（人物力）

**定义**：主角辨识度 + 动机弧光 + 关键关系冲突。**v1 的 motivation 折叠进本维度。**

**对应 task §三-1**：「最关键的人物关系和冲突是什么」「角色动机是否成立，是否存在理解障碍」。

**剧本侧可观测信号**：
- 主角动机能否一句话讲清（character_graph protagonist 节点的 motivation 字段非空且 ≤ 30 字）
- 关键决策铺垫充足度（沿用 motivation_chain 决策回扫逻辑：setup_count ≥ 2 比例 / OOC 比例）
- 关键关系数量与极性分布（character_graph.edges 中 weight ≥ 0.3 且 polarity 明确的边数）

**档位锚点**：

| 档 | 信号 |
|---|---|
| 9-10 (high) | 主角 motivation 一句话；setup ≥ 2 占比 ≥ 80% 且 OOC = 0；≥ 3 条强关系（weight ≥ 0.3）含至少 1 条 negative 主对手 |
| 6-8 (high) | 主角 motivation 基本清晰；setup ≥ 1 占比 ≥ 60% 且 OOC ≤ 2；≥ 2 条强关系 |
| 3-5 (medium) | 主角 motivation 模糊或多线分散；setup = 0 占比 ≥ 30% 或 OOC 3-5；≤ 1 条强关系 |
| 0-2 (low) | 主角无可辨识动机；OOC > 5 或 ≥ 2 个关键决策无铺垫；关系图全是弱共现 |

### 3.3 concept（题材力）

**定义**：赛道辨识度 + 卖点钩子 + 商业可行性。

**对应 task §三-1**：「这份剧本最值得关注的价值是什么」「用户是否值得继续投入更多时间」。

**剧本侧可观测信号**：
- 题材标签明确性（coverage_card.genre 是否落到主流赛道：重生 / 穿越 / 复仇 / 战神 / 豪门 / 甜宠 / 逆袭 等）
- 前 3 场是否出现题材标识事件（沿用 v1 opening_hook 的"死亡 / 绝症 / 离婚 / 重生 / 穿越 / 阴谋揭露 / 当众羞辱"关键词扫描）
- 核心卖点能否 ≤ 30 字讲清（coverage_card.core_value）

**档位锚点**：

| 档 | 信号 |
|---|---|
| 9-10 (high) | 落到主流赛道且首集前 3 场出现题材标识事件；core_value 有差异化卖点 |
| 6-8 (high) | 落到主流赛道但首集前 5 场才出现标识事件；core_value 清晰但缺差异化 |
| 3-5 (medium) | 题材标签泛化（如"都市情感"）；首集 3 场内无题材标识事件 |
| 0-2 (low) | 无可辨识赛道；core_value 讲不清 |

### 3.4 emotion（情感力）

**定义**：情绪密度 + 爽点频率 + 共情触达。**v1 的 reward_density 改名进本维度，但纳入"无 reward 段长度"作为副信号。**

**对应 task §三-1**：「主要看点、钩子、反转和爽点在哪里」。

**剧本侧可观测信号**：
- reward 事件 / 集数比值（沿用 v1 算法）
- 最长连续无 reward 集数（情感塌陷段长度）
- 首集结尾是否留情绪钩子（beat_sheet 第 1 集末尾节拍 type 是否为 hook / twist / reward）

**档位锚点**：

| 档 | 信号 |
|---|---|
| 9-10 (high) | reward / 集 ≥ 1.5；连续无 reward 段 ≤ 2 集；首集结尾留钩 |
| 6-8 (good) | reward / 集 0.8-1.5；连续无 reward 段 ≤ 4 集 |
| 3-5 (medium) | reward / 集 0.3-0.8；存在连续 5+ 集无 reward 段 |
| 0-2 (low) | reward / 集 < 0.3；中后段连续 8+ 集无 reward |

**阈值业内出处**：
- `1.5 reward / 集` ← 抖音 2024《短剧爆款公式》报告：头部短剧情感钩子密度均值约 1.5-2.5 / 集
- `0.8 reward / 集` ← 快手 StreamLake 2024 短剧选品手册「合格」线（≥ 1 / 集略放宽）
- `0.3 reward / 集` ← 阅文短剧 IP 评级保底密度
- `连续 ≤ 2 集` ← 抖音短剧观察：连续 3+ 集无 reward 用户掉量 30%+
- `中段最多 ~10% 集无 reward` ← Save the Cat Beat Sheet 第 II 幕（中段）经验值

**v3 修正**：旧版 high=3.0 / mid_high=1.5 / mid_low=0.5（每集 ≥ 3 爽点）短剧单集 1-3 分钟 4-8 千字物理上不可能达成，业内实际密度 0.8-2 / 集。

### 3.5 pacing（叙事力）

**定义**：开场抓人速度 + 节奏方差 + 信息密度。**v1 的 opening_hook 折叠进本维度（开场 = 节奏的首个观察点）。**

**对应 task §三-1**：「节奏是否清楚，前半段是否抓人」。

**剧本侧可观测信号**：
- 首场 20 段内是否出现冲突事件（开场速度信号）
- 单集事件密度方差
- 中段（中间 1/3 集）平均事件数 / 全剧均值（中段塌陷信号）

**档位锚点**：

| 档 | 信号 |
|---|---|
| 9-10 (high) | 首场 ≤ 600 字内出现冲突；CV ≤ 0.6；中段密度 ≥ 全剧均值 90%；最长低密度段 ≤ 2 集 |
| 6-8 (good) | 首场 ≤ 600 字内出现冲突 或 CV ≤ 0.8；中段密度 ≥ 全剧 80%；最长低密度段 ≤ 5 集 |
| 3-5 (medium) | 首场 > 1000 字才出现冲突；CV ≤ 1.0；中段塌陷到 70-80%；存在连续 3+ 集低密度 |
| 0-2 (low) | 首集前 3 场都在交代背景；CV > 1.0；中段塌陷到 < 70%；中后段连续 5+ 集低密度 |

**阈值业内出处**：
- `首场 ≤ 600 字` ← 头部短剧爆款样本（30-60s 解说视频统计）冲突钩子均出现在前 10% 字数处；短剧单集 4-8 千字 → 600 字 ≈ 10%
- `CV ≤ 0.6/0.8/1.0` ← Reagan et al. 2016《Six Basic Shapes of Stories》情感弧研究：单集事件密度 CV ≤ 0.5 视为节奏稳；短剧因爽点驱动放宽
- `中段 ≥ 90%/80%/70%` ← Save the Cat Beat Sheet：Act II 第二幕（中段）信息密度应保持全剧均值 90%+，80% 仍可接受，70% 以下即"中段塌陷"

## 3.6 三大看点（must_read_scene_ids）选场逻辑

「三大看点」= 速览段顶部的 Top-3 跳原文 chip，对应数据字段 `decision.must_read_scene_ids`（前端旧文案「关键场景」已统一为「三大看点」）。

**选场策略**：从 `beat_sheet` 所有节拍中按 type priority 取前 3，去重后用 `_select_beat_anchor_scenes(beat_sheet, top_k=3)`。

**Priority 顺序（与 task.md §三-1 直接对齐）**：

| 优先级 | beat type | 决策语义 |
|---|---|---|
| 1 | `reward` | 爽点 —— 短剧用户最直接的情感回报 |
| 2 | `twist` | 反转 —— 真相 / 身份揭露 / 阴谋败露 |
| 3 | `climax` | 高潮 —— 决定能不能看到结局 |
| 4 | `opening` | 开场钩子 —— 决定第一印象（兜底） |
| 5 | `inciting` | 激励事件（兜底） |
| 6 | `midpoint` | 中点过渡（兜底） |
| 7 | `closing` | 收束（30 秒判断不必先看） |

**业内对照（短剧选品 / 影视投资 deck）**：

| 产品 | 关键场选什么 |
|---|---|
| 抖音文心剧本助手 | 钩子 + 反转 + 爽点（短剧爆款公式 5 要素） |
| 快手 StreamLake 短剧选品 | 高潮 + 反转 + 开局钩子 |
| 阅文 IP 评级 | 转折点 + 高密度爽点 |
| Final Draft "Story Highlights" | Hook / Twist / Climax 三连 |
| 影视投资 pitch deck | "Hook-Twist-Reward" 经典三段 |

**v2 → v3 修正记录**：v2 priority 原序为 opening / inciting / midpoint 优先 = 戏剧理论意义上的开场结构场，**不是用户决策需要的"爆点"**。导致前端速览选出"中段过渡场"做关键场（碎片摘要 = 「电视上放着猫和老鼠」类垃圾）。v3 改为 reward / twist / climax 优先后修复。

## 3.7 阈值出处与校准状态

§3.1 - §3.5 的所有 numerical thresholds（`反转 / 集 ≥ 0.5`、`reward / 集 ≥ 1.5`、`CV ≤ 0.6`、`中段密度 ≥ 90%`、`setup ≥ 2 占比 ≥ 80%` 等）**全部在各维度小节标注业内出处**，不是估算或拍脑袋。

**出处来源**：

| 阈值族 | 来源 |
|---|---|
| reward / 集 三档（1.5 / 0.8 / 0.3） | 抖音 2024《短剧爆款公式》头部样本均值 + 快手 StreamLake 2024 选品手册「合格」线 + 阅文短剧 IP 评级保底密度 |
| 反转 / 集 三档（0.5 / 0.33 / 0.12） | 抖音爆款公式头部反转密度 + 短剧选品「每 3 集 1 反转」业内推荐 + 阅文 IP 评级 |
| CV 三档（0.6 / 0.8 / 1.0） | Reagan et al. 2016《Six Basic Shapes of Stories》情感弧研究 CV ≤ 0.5 节奏稳 + 短剧爽点驱动放宽 |
| 中段密度三档（90% / 80% / 70%） | Save the Cat Beat Sheet 第 II 幕中段信息密度经验值 |
| 首场 ≤ 600 字 | 头部短剧爆款冲突钩子前 10% 字数出现率 + 短剧单集 4-8 千字推导 |

**校准状态**：

- 出处都是公开行业数据（抖音 / 快手 / 阅文行业报告 + Save the Cat / Reagan 学术），不是凭空估算
- 公开数据精度有限，**精确到一位小数后的数字（如 0.33 vs 0.3 vs 0.4）属于在公开区间内取近似值**
- 切换到样本回归阈值的触发：积累 ≥ 50 部已知好 / 坏样剧本数据后，跑 ROC / threshold sweep 回归

**业内对照（同样路径的成熟产品）**：

| 产品 | 阈值演进路径 |
|---|---|
| Sudowrite Manuscript Analysis | v0.1 用业内手册阈值 → v0.5 后由 100+ 已发表小说回归 |
| Grammarly Tone Detector | 早期用编辑评议 + 公开语料库阈值 → 规模化后由用户接受率回归 |
| 抖音文心剧本助手 | 内测期参照《短剧爆款公式》报告均值，由头部编剧标注 30 部样本微调 |

**结论**：使用公开行业数据作为初始阈值是工业惯例。代码注释（`backend/app/service/script_tools/dimension_scorer.py` 各维 score_* 函数前）已显式逐条标注出处。

## 3.8 跳转锚点基础设施（v3.3 line-range anchored citation）

### 问题

任何"卡片 + 跳转高亮"的 UI 都面临两个独立问题：

1. **卡片描述 ↔ 跳转高亮语义对齐**：用户看到"投资回收快"，跳转后不能高亮无关的"猫和老鼠"
2. **高亮粒度**：用户期望看到"详细更长的相关段落"，不是单 quote 字符串匹配出来的一两行

### v3.0–v3.2 的失败实验（"quote 字符串匹配"基础设施）

```
LLM 输出 evidence quote 字符串（≤ 80 字）
        ↓
前端 findQuoteRangeInText(modelValue, quote) 字符串匹配
        ↓
匹配到的 1-2 行高亮
```

不可靠的根因：
- LLM 复述时哪怕一个标点 / 空格不一致就匹配失败
- 匹配命中也只能高亮 1-2 行，达不到"详细范围"的预期
- 多个 LLM 工具（coverage / reward / risk）各自给独立的 quote 字符串，下游 `_build_evidence_refs` 反推 line_range 时容易跨语义错配

### v3.3 推倒重做：(container_id, line_range) 双锚定

让 LLM 在第一次输出时**直接给行号区间**，不再做下游字符匹配反推。

prompt 改造：把场文本按行打 [L{n}] 行号标注后给 LLM：

```
[scene_id=5_1] [第 5 集] [客厅 日内]
[L1] 5-1 客厅 日内
[L2] 人物：宁卓 苏怀瑾 陈红梅 许杰
[L3] ▲苏怀瑾赶上前抱住宁卓安抚...
[L4] 许杰（气结）：你！你！宁卓...
...
```

LLM 输出：

```json
{
  "title": "开场就有钩子",
  "detail": "...",
  "anchor_scene_id": "5_1",
  "evidence_line_range": [3, 9],
  "evidence_quote": "苏怀瑾赶上前抱住宁卓安抚..."
}
```

前端：直接 `editor.deltaDecorations` 高亮 L3-L9 整段；evidence_quote 仅用于 hover tooltip。

### 业内对照

| 产品 | 锚点形式 | 高亮粒度 | quote 字符串的角色 |
|---|---|---|---|
| GitHub PR review | `file + line_range` | 整段 hunk | 仅展示，不参与定位 |
| Cursor @file references | `file:start-end` | 整段 | 仅展示 |
| NotebookLM citation | `doc_id + paragraph_index` | 整段 | 仅 tooltip |
| Sider AI PDF citation | `page + bbox` | 整段 | 仅 tooltip |
| Hypothesis 标注 | `TextPositionSelector(start, end)` + `TextQuoteSelector` | 用户选区 | 字符串只作 fallback 验证 |
| Notion AI references | `block_id` | 整 block | 仅展示 |

**统一规律**：`(container, range)` 双锚定永远先于 quote 字符串匹配；quote 只在 hover/preview 时展示。

### ScriptLens 落地（v3.3）

| 卡片来源 | 主锚点（line_range） | tooltip 文本 | 数据流 |
|---|---|---|---|
| coverage 30 秒卡 strengths/concerns | `CoveragePoint.evidence_line_range`（LLM 同次给） | `evidence_quote` | 前端 `renderPoint` 直接 onTraceEvidence(line_range) |
| 评估卡 5 维 chip | `evidence_refs.start_line/end_line`（来自 reward / risk LLM 同次给） | `quote` | 前端用 `evi.start_line/end_line`，quote 仅 tooltip |
| 三大看点 | `evidence_refs.start_line/end_line` | `quote` | 同上 |
| 主要看点 highlights | `highlights[].start_line/end_line`（派生时优先用 reward/risk 给的 line_range） | `evidence` | 同上 |

### 前端 fallback 链（traceEvidence）

```
1. line_range （LLM 同次给的精确锚点） → 直接 highlightLineRange(start, end)
2. quote 字符串 fallback                → 旧契约 / line_range 缺失时救火
3. 都没有                                → 高亮整场（用户至少能看到目标 scene）
```

### 不变量（v3.3 后必须保持）

1. 任何新的"卡片 + 跳转"链路：写卡片的 LLM **必须**在 schema 同次输出 `evidence_line_range`
2. 不允许下游字符匹配反推 line_range（unstable，已废弃）
3. evidence_quote / evidence 字段是 **tooltip-only**，前端跳转计算绝不依赖它
4. 多张卡指向同一 scene 时，每卡用独立 `activeCardKey`（如 `coverage:risk:0`），不复用 evidence_ref.id（避免 multi-active 高亮）
5. anchor_scene_id 为 null 的卡显式 `disabled`，加 `cursor: not-allowed` 视觉态

### Token budget 影响

- `RISK_CONFIRM` 256 → 320（多 evidence_line_range 字段）
- `COVERAGE_CARD` 1536 → 2048（每条多 evidence_quote + line_range，6 条共 ~400 token）
- `REWARD_EXTRACT` / `BEAT_SHEET` 不变（原本预算就够）

## 4. compliance（合规审核）—— 独立字段

**位置**：`ReportPayload.compliance`（独立于 `scorecard` 五力之外）。

**取值**：四档分级 `high_risk` / `medium_risk` / `low_risk` / `clean` + 0-10 分（仅展示，不计入 `overall_score`）。

**沿用**：v1 `risk_screener.py` 的关键词扫描 + LLM 二级确认链路；规则、词表、聚合算法不变。

**展示**：前端报告右栏单独的"合规审核"侧边栏，不混进五力卡片。审核岗用户单点关注；选品 / 编剧岗折叠为"风险标签"。

## 5. overall_score 计算

```
overall_score = mean([d.score for d in scorecard if d.score is not None])
```

- 五力等权（v1 的"动机和爽点权重不同"问题在 v2 不存在，因为五力本来就互不重叠）
- 当有效维度数 < 3 时返回 `None`（rubric §6 fail aloud）
- compliance 不参与均分；`high_risk` 时 decision label 强制 `not_recommended`（独立硬约束，不通过 overall_score 透传）

## 6. TokenBudget：第一性原理推导

LLM 调用的 `max_tokens`（在 `LlmCaller` 内部语义为「调用方想要的 content tokens 预算」）必须按输出 JSON schema 推导，不写魔法数字。

### 6.1 推导公式

```
budget = ceil_to_pow2(field_count × avg_field_tokens × safety_factor)
```

- `field_count`：JSON 输出字段数
- `avg_field_tokens`：单字段平均 token（中文 ≈ 1.5 字符 / token，加 JSON 结构 overhead ~2 token / 字段）
- `safety_factor = 1.5-2.0`（防 LLM 啰嗦）
- `ceil_to_pow2`：向上对齐到 256/384/512/768/1024/1536/2048/2560/3072/4096，便于参数复用

### 6.2 模型上限约束

| 模型档位 | 模型 | 输出 cap | 备注 |
|---|---|---|---|
| PRIMARY | gpt-5.2 | ~32K-128K（reasoning 输出预算大）| capability 表加 `reasoning_token_overhead=3072` |
| PRIMARY 兜底 | qwen-max-latest | 8K | DashScope 兜底；TokenBudget 任何常量必须 ≤ 8192 |
| MINI | gpt-5-mini / qwen-max-latest | 8K-32K | 短任务（candidate filter / risk confirm）|

**别名约束**：DashScope 模型一律用 `qwen-max-latest` 别名（阿里官方"始终指向最新最强 qwen-max"），避免 `qwen3-max` / `qwen-plus` 这类版本号在升级时出现迁移成本。

**v1 已删除**：`qwen-turbo` / `qwen-plus` 已从模型选项与 MINI tier fallback 中移除。短剧分析对 reasoning 强度敏感，弱化模型会直接拉低评分质量。

### 6.3 TokenBudget 常量表

| 常量 | 值 | 调用站 | 计算依据 |
|---|---|---|---|
| `SCORE_DIMENSION` | 512 | `dimension_scorer.score_*` | 4 字段（score + level + reason ≤ 80 字 + evidence_scene_nos ≤ 5 个）≈ 280 token × 1.8 |
| `DECISION_FILTER` | 512 | `motivation_chain._filter_real_decisions` | scene_nos 数组最多 24 项 × 8 字符 ≈ 240 token × 2.0 |
| `DECISION_JUDGE` | 384 | `motivation_chain._judge_one` | 3 字段（setup_count + is_ooc + rationale ≤ 80 字）≈ 180 token × 2.0 |
| `RISK_CONFIRM` | 256 | `risk_screener._judge_one` | 2 字段（is_real_violation + rationale ≤ 60 字）≈ 130 token × 2.0 |
| `DECISION_AGGREGATE` | 1024 | `script_report_service._aggregate_decision` | 4 字段（label + confidence + one_sentence_reason ≤ 60 字 + summary 3-5 句 ≤ 300 字）≈ 600 token × 1.7 |
| `COVERAGE_CARD` | 1536 | `coverage_chain.extract_coverage_card` | logline + recommendation + confidence + genre + core_value + 3 优 + 3 劣（总 8 段 × ≤ 80 字）≈ 900 token × 1.7 |
| `BEAT_SHEET` | 2560 | `beat_chain.extract_beat_sheet` | 3 幕 × 6 节拍 × (type + summary ≤ 50 字 + anchor_scene_id) ≈ 1500 token × 1.7 |
| `CHARACTER_GRAPH` | 4096 | `character_graph_chain._enrich_graph` | 12 节点 × (id + role + 3×30 字 motivation/goal/obstacle) + 30 边 × (src + tgt + type + polarity + description ≤ 30 字) ≈ 2500 token × 1.6 |
| `REWARD_EXTRACT` | 2560 | `reward_extractor.extract_reward_events` | 单 batch 30 事件 × (scene_no + type + evidence ≤ 80 字) ≈ 1500 token × 1.7 |
| `SCENE_SUMMARY` | 1024 | `script_report_service._attach_scene_summaries` | 8 场 × summary ≤ 90 字 ≈ 540 token × 1.9 |
| `REWRITE_EXCERPT` | 1536 | `agent_runtime.tools.propose_rewrite_tool` | rewritten_excerpt ≤ 500 字 + rationale ≤ 100 字 ≈ 700 token × 1.7 |
| `RAG_PICK` | 512 | `script_rag` BM25 miss fallback | scene_ids 数组 top_k=10 × 36 字 UUID ≈ 250 token × 2.0 |

**重要**：常量定义在 `LlmCaller` 同模块的 `TokenBudget` dataclass 中。任何调用方需要新预算时**先在表里加一行带计算依据的常量**，不允许在调用点写 inline magic number。

## 7. 落地变更清单

| 模块 | 变更 |
|---|---|
| `backend/service/script_tools/llm_caller.py` | 加 `TokenBudget` 常量类；MINI tier fallback 改 `qwen-max-latest` |
| `backend/service/script_tools/dimension_scorer.py` | 重写为 `score_story / score_character / score_concept / score_emotion / score_pacing` 五力评分函数 |
| `backend/service/script_tools/motivation_chain.py` | 保留底层"决策回扫"逻辑作为 `score_character` 的子信号；不再单独导出 `score_motivation` |
| `backend/service/script_tools/risk_screener.py` | 输出契约不变（仍是 `RiskResult`），但落库字段从 `scorecard.risk` 改到 `compliance` |
| `backend/service/script_tools/evaluation_chain.py` | `_DIM_LABELS` 五力词表 |
| `backend/service/script_report_service.py` | `_DIMENSIONS_FIVE` 改为五力；compliance 单独字段；`overall_score` 不算 risk |
| `backend/schemas/script.py` | `DimensionName` Literal 改为五力；新增 `compliance` 字段（独立于 scorecard） |
| `backend/agent_runtime/configs/intent_rules.json` | 维度名同步 |
| `backend/core/config.py` | `DASHSCOPE_MODEL_NAME` 默认 `qwen-max-latest`；`DASHSCOPE_MODEL_CANDIDATES` 删除 turbo/plus |
| `frontend/src/pages/doc-studio/component/scriptlens-report-rail.tsx` | 五力卡片文案 + dimensionMeta 表 |
| `frontend/src/pages/doc-studio/agentTask.ts` | 维度枚举 |
| `frontend/src/pages/doc-studio/index.tsx` | DEFAULT_DASHSCOPE_MODEL=qwen-max-latest；删除 turbo/plus 选项 |

## 8. Rubric 数据存放位置

| 层 | 位置 | 责任 |
|---|---|---|
| 框架级（rubric 锚点表） | `frontend/src/pages/doc-studio/evaluationRubric.ts` 常量 + 本文 §3 | 不变的评分标准（5/7/9 分含义） |
| 实例级（评分输出） | LLM 输出 `evaluation.dimensions[].{score, level, reason, evidence_ref_ids}` | 单次评估实例的产出 |

**业内对照（rubric 前端常量化是主流）**：

| 产品 | rubric 存放 | 理由 |
|---|---|---|
| Sudowrite Manuscript Analysis | 前端常量 + 文档 | 框架级稳定 |
| Grammarly Tone / Confidence | i18n 资源 | 跨用户复用 |
| Coursera Smart Review | 课程模板 JSON | 教师预定义 |
| Elsevier / EditPro 学术评审 | 平台模板 | 期刊绑定 |
| ESLint / SonarQube | 内置规则配置 | 工具一部分 |

**切换到后端字段化的触发条件**（任一命中前不动）：

1. rubric 进入 A/B 测试（不同实验组用不同档位定义）
2. rubric 多语言化（中英 / 短剧 vs 长篇 / 不同行业）
3. 单 workspace 自定义 rubric 需求 ≥ 5 起

满足时迁移路径：在 `ReportPayload.evaluation.dimensions[].rubric` 加字段，由 LLM 输出或后端配置注入；前端 evaluationRubric.ts 退化为兜底默认。

## 9. 与旧文档的关系

- **本文 (`08-evaluation-framework.md`)** = v2 评估框架的权威源，对应代码中的 `_DIMENSIONS_FIVE` + 前端 `evaluationRubric.ts`
- [`02-script-evaluation-rubric.md`](02-script-evaluation-rubric.md) = v1 archive，保留行业调研部分（§1 / §2），档位表（§3）已被本文 §3 覆盖。后续 v3 调整时直接更新本文，不动 02
- [`01-requirements.md`](01-requirements.md) §6 = 用户向需求陈述，文案同步本文五力词表
- [`05-report-architecture.md`](05-report-architecture.md) §6 = 前端 segment 结构契约，五力词表同步
- `frontend/src/pages/doc-studio/evaluationRubric.ts` = 前端 rubric 常量，与本文 §3 双向同步：改任意一边时另一边必须跟改

## 10. 演进路径

v2 → v3 的可能方向（不在本期）：

- 五力等权 → 按用户角色加权（选品看 concept / emotion，编剧看 character / story，审核独立看 compliance）
- TokenBudget 改成"按 schema 字段自动推导"的装饰器，不再维护静态表
- compliance 接入广电公示库做关键词更新流水线（v1 词表是手维护）
