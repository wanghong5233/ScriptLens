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
- 反转事件密度（reward_events.event_type ∈ {reversal, face_slap, scheme_exposed} 计数 / 总集数）
- 钩子节点完整性（beat_sheet 是否含 opening / inciting / midpoint / climax / closing 五个关键节拍）

**档位锚点**：

| 档 | 信号 |
|---|---|
| 9-10 (high) | logline 清晰；反转 / 集 ≥ 2.0；五个关键节拍完整 |
| 6-8 (high) | logline 基本清晰；反转 / 集 1.0-2.0；缺 ≤ 1 个关键节拍 |
| 3-5 (medium) | logline 模糊；反转 / 集 0.3-1.0；缺 ≥ 2 个关键节拍 |
| 0-2 (low) | 主线讲不清；反转 / 集 < 0.3；缺 climax 或 closing 节拍 |

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
| 9-10 (high) | reward / 集 ≥ 3.0；连续无 reward 段 ≤ 1 处；首集结尾留钩 |
| 6-8 (high) | reward / 集 1.5-3.0；连续无 reward 段 ≤ 3 处 |
| 3-5 (medium) | reward / 集 0.5-1.5；存在连续 5+ 集无 reward 段 |
| 0-2 (low) | reward / 集 < 0.5；中后段连续 8+ 集无 reward |

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
| 9-10 (high) | 首场 ≤ 20 段内出现冲突；方差小；中段平均 ≥ 全剧均值 90% |
| 6-8 (high) | 首场 ≤ 30 段内出现冲突；方差中等；中段平均 ≥ 全剧均值 80% |
| 3-5 (medium) | 首场 > 30 段才出现冲突；存在连续 3+ 集低密度段；中段塌陷（< 70%）|
| 0-2 (low) | 首集前 3 场都在交代背景；中后段连续 5+ 集低密度 |

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
