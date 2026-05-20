# ScriptLens 产品需求

> **最高准则是 [`source/task.md`](../source/task.md)**，PRD 是 task.md 的工程化落地，可随开发演进修订。
> - 报告内部结构（4 segment / 故事/人物/评估）契约见 [`05-report-architecture.md`](../architecture/05-report-architecture.md)
> - 存储层当前实现与 SQLite + FTS5 演进方向见 [`06-storage-architecture.md`](../architecture/06-storage-architecture.md)
> - 解析质量评估方法见 [`07-evaluation.md`](../playbook/07-evaluation.md)
> - UI 与 Agent 协作落地心智见 [`03-system-mental-model.md`](../architecture/03-system-mental-model.md)
>
> 当 PRD 与 05 / 06 / 03 冲突时：**新结构以专题文档为准，PRD 同步修订**；当 PRD 与 task.md 冲突时：**task.md 为准**。

## 1. 一句话定位

ScriptLens 是面向**短剧选品 / 编剧统筹 / 平台审核**的爆款短剧分析 Agent。用户在 30 秒到 10 分钟时间预算内，对一份陌生长剧本形成「是否值得继续投入」的可验证判断，每个判断都能回到原文场景。

「理解」≠ 摘要 ≠ 复述剧情。「理解」= 把影响判断的信息提取、组织、定位、解释。

## 2. 真实数据基线

测试集 `eval/短剧剧本/爆款短剧剧本（完整本）/` 共 43 份真实爆款短剧。架构必须满足以下硬约束：

| 维度 | 实测事实 | 对架构的硬约束 |
|---|---|---|
| 文件格式 | docx 23 份 / pdf 18 份 / 老 doc 2 份 | MVP 支持 docx + pdf（覆盖 41/43 = 95%）+ txt / md 兜底；老 doc（2 份）不支持，提示用户另存为 docx |
| 单份长度 | 88KB–29MB；DOCX 普遍 3000–5000 段 / 6–15 万字；PDF 50–160 页 | 单文档 chunk 数量 500–2000，pgvector 单表足够，不需要 ES |
| 顶部结构 | 多数含「故事大纲 + 人物小传 + 100 集分场剧本」三段式 | 解析器要识别「大纲段」与「正文段」分别处理 |
| 正文格式 | `第 N 集` → `1-1 沈宅 夜 内` → `人物：A、B` → `▲场景描述` → `角色 (OS/VO/OV)：对白` | segmenter 必须识别 `第N集` / `X-Y 场号` / `角色：对白` / `角色 os：内心` 四类锚点 |
| 噪音 | 顶部含网址、版权说明、目录、PDF 反爬层乱码 | 解析后必须做噪音剥离 |

## 3. 真实用户与真实痛点

题目背景列了 6 个角色（策划/统筹/运营/选品/投放/审核）。MVP 只服务最高频且最舍得为「判断质量」付费的 3 个：

| 角色 | 关心的判断 | 错判的代价 |
|---|---|---|
| **选品 / 内容采购** | 这本是否值得签？前 5 集是否抓人？爽点是否密集？ | 签错一本浪费几十万拍摄成本 |
| **编剧统筹 / 改稿** | 哪些桥段动机不成立？节奏在哪里拖？低分段能怎么改？ | 改错方向导致重写 |
| **平台审核 / 风控** | 是否有暴力、伦理、价值观、审核敏感表达？分布在哪几集？ | 审核驳回直接下架 |

策划 / 运营 / 投放视角作为「视角切换」加分项接入，不进 MVP 主链路。

## 4. 不是普通摘要的根因

| 摘要回答 | 真实工作判断需要回答 |
|---|---|
| 这部剧讲了什么 | 前 5 集第几个钩子在哪一场？强度够不够？ |
| 主要人物是谁 | 男主在第 8 集的转变动机能否成立？原文哪一段支撑？ |
| 冲突是什么 | 高潮在第几集？是否过早或过晚？ |
| 风险有哪些 | 暴力 / 伦理 / 价值观风险具体落在哪一集哪一场？ |

摘要做"压缩"，工程判断做"带证据的指标定位"。**任何无原文锚点的判断在本系统都视为无效输出。**

## 5. 必须回答的 5 个核心问题

| # | 问题 | 输出形态 |
|---|---|---|
| Q1 | 这本剧前 5 集是否抓人？ | 决策卡（推荐 / 谨慎 / 不建议）+ 一句话理由 + 必读 3 场 |
| Q2 | 5 个决策维度各打几分？依据是什么？ | scorecard，每条带 `evidence_ref_ids` |
| Q3 | 某个判断的原文依据是什么？（追问） | Agent 多轮，回答必须带证据片段 |
| Q4 | 哪 1-2 个低分段最值得改？怎么改？ | 改写工具输出原文 + 改写版 + diff + 解释 |
| Q5 | 不同岗位关心什么？ | 「行动」segment 同时呈现三张 Persona Action Card（选品 / 编剧 / 审核），每张卡 = 一句话结论 + 优先证据 ≤3 + Next Action ≤3，详见 [`09-action-lens.md`](../architecture/09-action-lens.md) |

Q1–Q4 是 MVP 必做，Q5 是加分项。

## 6. 5 维数据评估卡（task.md §五 3 加分项实装）

5 维评分是 task.md §五 3「等级判断或量化分析」的具体实装，**作为深度层「评估」segment 的内容**，不抢 30 秒决策位（[`05-report-architecture.md §6`](../architecture/05-report-architecture.md#6-前端-4-segment)）。

| 维度 | 0-10 评分依据 | 失败模式 |
|---|---|---|
| **opening_hook**（开场钩子强度） | 前 5 集内危机 / 冲突 / 反差事件密度；首场是否在 1 分钟内建立矛盾 | 前 5 集都在交代背景 → 低分 |
| **reward_density**（爽点密度） | 全剧反转、打脸、逆袭、CP 互动事件每 10 集出现次数 | 50 集才 2 次 → 低分 |
| **motivation**（动机自洽度） | 主角关键决策是否有可追溯的因果；OOC 次数 | 男主毫无理由原谅反派 → 低分 |
| **pacing**（节奏控制） | 单集事件密度方差；是否有连续 5 集低密度段 | 第 30-40 集全是回忆 → 低分 |
| **risk**（审核风险等级） | 暴力 / 伦理 / 性 / 价值观 / 政策敏感词命中数与场景数 | 含未成年涉性描写 → 直接 high_risk |

每个评分必须给出 `reason` + `evidence_ref_ids[]`。无证据的评分视为 LLM 幻觉，丢弃。

不做 8-9 维。维度多 = 模型瞎打分。

**工业判据 / 档位锚点 / prompt 模板 / 失败模式细则见 [`02-script-evaluation-rubric.md`](02-script-evaluation-rubric.md)。** 该文档基于抖音 / 快手 / 广电 / 短剧反转工业指标做了第一性原理推导，是评分 Agent 实装的 prompt 来源。

## 7. Agent 输出契约

> **本节是 5 维评分契约的稳定基线**。`coverage_card` / `beat_sheet` / `character_graph` / `pacing_curve` 等扩展字段（v3）在 [`05-report-architecture.md §4`](../architecture/05-report-architecture.md#4-数据契约) 定义，不在本表，保持 PRD 稳定。

```jsonc
{
  "script_id": "uuid",
  "title": "string",
  "decision": {
    "label": "recommend_continue | cautious_continue | not_recommended",
    "confidence": "high | medium | low",
    "one_sentence_reason": "string",
    "must_read_scene_ids": ["scene_001", "scene_017", "scene_042"]
  },
  "scorecard": [
    {
      "dimension": "opening_hook | reward_density | motivation | pacing | risk",
      "score": 0,                       // 0-10
      "level": "high | medium | low | high_risk",
      "reason": "string",
      "evidence_ref_ids": ["evi_xxx"]   // 必填，非空
    }
  ],
  "evidence_refs": [
    {
      "id": "evi_xxx",
      "scene_id": "scene_017",
      "scene_label": "5-3 沈宅 夜 内",
      "start_line": 1234,
      "end_line": 1289,
      "quote": "≤90 字原文片段",
      "reason": "为何这段支撑该判断",
      "confidence": "high | medium | low"
    }
  ],
  "characters": [
    {"name": "string", "first_appear_scene_id": "...", "role": "..."}
  ],
  "rewrite_suggestions": [
    {
      "scene_id": "scene_022",
      "target_dimension": "motivation",
      "issue": "string",
      "original_excerpt": "string",
      "rewritten_excerpt": "string",
      "diff": "unified diff format",
      "rationale": "string"
    }
  ]
}
```

视角切换由「行动」segment 的三张 Persona Action Card 实装；`scorecard` / `must_read_scene_ids` 在底层契约里顺序固定，不按角色重排（详见 [`09-action-lens.md`](../architecture/09-action-lens.md)）。

**v3 扩展字段**（`coverage_card` / `beat_sheet` / `character_graph` / `pacing_curve` / `evaluation`）契约见 [`05-report-architecture.md §5`](../architecture/05-report-architecture.md#5-数据契约)；本节是 v1 基线契约，过渡期内 `scorecard` 与 `evaluation.dimensions` 同源。

**报告 UI 分层与 4 segment 组织（速览 / 故事 / 人物 / 评分）见 [`05-report-architecture.md §5`](../architecture/05-report-architecture.md#5-前端-4-segment)；本节 §7 是底层契约，不锁定 UI 形态。**

## 8. 必须支持的交互

| 交互 | 协议 |
|---|---|
| 上传剧本（docx / pdf / txt / md） | `POST /api/scripts` 多部分文件 |
| 异步解析进度 | `GET /api/jobs/{id}` 轮询 |
| 查看结构化报告 | `GET /api/scripts/{id}/report` |
| 多轮追问 | `POST /api/scripts/{id}/chat`，SSE 流式 + `Last-Event-ID` 重连。后端走 ReAct Agent（`app/agent_runtime/` 子包，in-process 调用，不起独立微服务），工具栈含 RAG / 评分 / 改写 / **联网检索（web_search）**；Agent 调用 web_search 的边界与 query 模式见 [`00-reuse-matrix.md §5.1`](../architecture/00-reuse-matrix.md#51-web_search_tool-短剧场景调用边界) |
| 证据高亮 | 报告中 `evidence_ref_ids` → 前端跳转左侧原文 + 高亮场景 |
| 改写片段 | `POST /api/scripts/{id}/rewrite` 带 `target_dimension` |
| 视角切换 | `GET /api/scripts/{id}/view`（无 `?role=` 参数）；前端「行动」segment 派生三张 Persona Action Card（选品 / 编剧 / 审核），契约见 [`09-action-lens.md`](../architecture/09-action-lens.md) |
| 反馈 / 修正 | `POST /api/scripts/{id}/feedback` 带 `scope`（general / dimension / rewrite / scene）+ `scope_ref` + `message`，写入 `script_feedback` 表，下次 chat 自动注入 prompt |

## 9. 边界与失效场景

| 输入特征 | 系统行为 |
|---|---|
| 无场景标记的纯小说体 | 退化为按段落兜底分段，`pacing` 维度不可用，明确标注 `confidence: low` |
| 单份 > 50 万字 | 拒绝（提示分册），不做静默截断 |
| 扫描版 PDF（无文字层） | MVP 不支持，明确报错，不假装能处理 |
| 老 `.doc` 二进制（Word 97-2003） | MVP 不支持。明确报错并提示「请用 Word / WPS 另存为 `.docx` 后重传」。43 份测试集中仅 2 份是老 doc，41 份 docx/pdf 已覆盖 95% 场景，不为 5% 引入 LibreOffice 系统依赖 |
| 完全无标点对话格式 | 抽不出对白，对应维度评分缺失 + 标注 |
| 用户问超出剧本本身（市场数据 / 法规 / 同类爆款 / 演员档期） | Agent 调 `web_search_tool` 联网检索，**返回结论必须列源 URL**；缺 `WEB_SEARCH_API_KEY` 时工具优雅降级（`{skipped, reason}`），Agent 终答里声明「网络信息暂不可用，本结论仅基于剧本本身分析」，不编造 |
| 用户问完全脱离短剧领域（如「帮我写代码」「今天天气」） | Agent 明确拒绝并说明 ScriptLens 服务边界 |

## 10. 加分项优先级

| 优先级 | 加分项 | 实现方式 |
|---|---|---|
| **P0** | 可部署可访问 | 复用既有 ECS（独立 schema `scriptlens` + Tunnel hostname `api-scriptlens.wh5233.me`） |
| **P0** | 前端展示 | 双栏：左原文带场景树 + 高亮，右 Agent 对话 + 报告 tab |
| **P1** | 数据分析能力 | §6 的 5 维 scorecard + 评分依据表 |
| **P1** | 低评级改写 | §7 的 `rewrite_suggestions` 工具 |
| **P1** | 联网检索能力 | task §六「真正可工作的 Agent」的关键支撑。Agent 工具栈含 `web_search_tool`（Tavily / Serper），覆盖剧本之外的查询：选品看市场 / 编剧查爆款 / 审核查法规 / 改写借参考。调用边界与 query 模式见 [`00-reuse-matrix.md §5.1`](../architecture/00-reuse-matrix.md#51-web_search_tool-短剧场景调用边界) |
| **P2** | 评估方法 | 3-5 份真实剧本人工标注 + 自动指标（证据召回率、维度分一致性）；详见 [`02-script-evaluation-rubric.md §5`](02-script-evaluation-rubric.md) |
| **P3** | 可进化 skill 机制（轻量实现） | 用户在报告 / 维度 / 改写 / 场景上提交反馈 → 写 `script_feedback` 表（按 scope 标记）→ 下次 chat 自动抽取为 3 个轻量 skill 槽（维度解释偏好 / 改写偏好 / 风险规避偏好）并注入 prompt，使 Agent 能感知用户偏好与历史修正。**不做完整 RL 训练 / reward model / skill 调度库** |

## 11. 非目标

- 不做完整影视项目管理
- 不做票房 / 播放量预测模型训练
- 不做剧本市场数据库对标
- 不做多用户协作权限
- 不做完整账号体系（沿用 `testuser` demo-entry 即可）
- 不做 6 个用户角色全套视角，只做 3 个 Persona Action Card（选品 / 编剧 / 审核）
- 不做全局视角 tab / 视角重排报告（数据 lens 假象，详见 [`09-action-lens.md §3`](../architecture/09-action-lens.md)）
- 不做 3 套时间预算分层（30s / 3min / 10min），单层报告 + 可下钻就够
- 不做完整 skill 调度库 / RL 训练 pipeline / reward model（仅 §10 P3 的轻量反馈注入）
- 不做老 `.doc` 二进制解析（提示用户另存为 docx）

## 12. 验收清单

基础（题目原文）：

- [ ] 上传一份真实爆款短剧 docx / pdf，5 分钟内出报告
- [ ] 报告含决策卡 + 5 维 scorecard，每条评分都有原文证据
- [ ] 用户可点击任一证据，左侧原文高亮跳转到对应场景
- [ ] 用户可多轮追问，Agent 回答必须引用原文
- [ ] 用户可对任一低分维度请求改写，输出原文 + 改写版 + diff
- [ ] 「行动」segment 同时呈现选品 / 编剧 / 审核三张 Persona Action Card，每张卡含一句话结论 + 优先证据 + Next Action（详见 [`09-action-lens.md`](../architecture/09-action-lens.md)）
- [ ] 用户可在报告 / 维度 / 改写 / 场景任一处提交反馈，下一次 chat Agent 能感知该反馈并据此调整回答

加分（按 §10 优先级）：

- [ ] 公网可访问 demo（一键 Demo 入口 → 公共 `testuser`）
- [ ] 前端双栏布局，证据高亮联动
- [ ] 5 份真实剧本跑过，scorecard 维度齐全
- [ ] README 说明：为什么不是普通摘要、5 维依据、失效场景、MVP 范围取舍
