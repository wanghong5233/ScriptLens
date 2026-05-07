# ScriptLens 解析质量评估

> 本文回答 `source/task.md` §五 2「如何评估这个助手的解析质量」。
> 评估目标不是证明模型永远正确，而是判断系统是否帮助用户更快、更稳地理解陌生长剧本。

## 1. 现状

评估采用「小样本人工标注 + 自动指标 + 用户任务验证」三层。人工标注负责判断质量上限，自动指标负责回归测试，用户任务验证负责证明实际有用。

```
真实剧本 3-5 份
      │
      ├─ 人工标注：coverage / beat / character / evidence / rewrite
      │
      ├─ 自动指标：证据召回率 / 关键场命中率 / 人物覆盖率 / schema 有效率
      │
      └─ 任务验证：30 秒判断 / 5 分钟理解 / 原文溯源 / 改写是否改善
```

## 2. 评估对象

| 模块 | 评估问题 | 对应 task.md |
|---|---|---|
| `coverage_card` | 是否能让用户 30 秒判断「值不值得继续读」 | §一 22-24 / §三 63-69 |
| `beat_sheet` | 核心主线、钩子、反转、高潮是否抓对 | §一 17 / 19 / 20 |
| `character_graph` | 关键人物关系和冲突是否完整、是否误连 | §一 18 / 21 |
| `evidence_refs` | 判断是否能回到正确原文场景和行 | §三 56-61 / 75-78 |
| `evaluation` | 质量、结构、节奏、风险评分是否有依据 | §五 3 |
| `rewrite_seeds` | 低评级剧本是否找到真正值得改的位置 | §五 4 |

## 3. 人工标注集

| 项 | 规模 | 标注内容 |
|---|---:|---|
| 剧本 | 3-5 份真实短剧 | 覆盖 docx / pdf、100 集分场、格式噪音、人物密集 |
| 关键场 | 每剧 5-8 场 | 开场钩子、激励事件、中点反转、高潮、收束、主要爽点 |
| 关键人物 | 每剧 3-8 人 | 主角、反派、关键配角、关系类型、动机、目标、阻碍 |
| 风险点 | 每剧 0-10 条 | 暴力、伦理、价值观、敏感表达 |
| 改写点 | 每剧 1-3 处 | 动机不成立、节奏拖、冲突弱、风险表达 |

标注文件形态：

```json
{
  "script_id": "uuid",
  "coverage": {
    "recommendation": "recommend|consider|pass",
    "core_value": "string"
  },
  "beats": [
    {"type": "opening", "scene_no": "1-1", "reason": "string"}
  ],
  "characters": [
    {"name": "string", "role": "protagonist", "relations": [{"target": "string", "type": "rival"}]}
  ],
  "risks": [
    {"scene_no": "string", "category": "string"}
  ],
  "rewrite_targets": [
    {"scene_no": "string", "dimension": "motivation", "issue": "string"}
  ]
}
```

## 4. 自动指标

| 指标 | 计算方式 | 合格线 |
|---|---|---:|
| 关键场命中率 | `beat_sheet` anchor 场命中人工标注关键场的比例；同集相邻场算 0.5 | ≥ 0.65 |
| 关键人物覆盖率 | `character_graph.nodes` 覆盖人工关键人物比例 | ≥ 0.80 |
| 关系边准确率 | `character_graph.edges` 中人工可确认关系的比例 | ≥ 0.60 |
| 证据可溯源率 | `evidence_refs.scene_id/start_line/end_line` 能打开并高亮到非空原文 | ≥ 0.95 |
| 评分有证据率 | 5 维 score 非空时 `evidence_ref_ids.length > 0` | 1.00 |
| schema 有效率 | `ReportPayload` 通过 Pydantic 校验 | 1.00 |
| 关键场摘要有效率 | 「关键场景」卡片展示整场概述，不展示单句 quote | ≥ 0.90 |
| 改写候选命中率 | `rewrite_seeds` 命中人工标注改写点或同场相邻维度 | ≥ 0.50 |

## 5. 用户任务验证

| 任务 | 验证方式 | 目标 |
|---|---|---|
| 30 秒判断 | 用户只看「速览」，说出是否继续读和一个理由 | 80% 用户能完成 |
| 5 分钟理解 | 用户看「故事 + 人物」，复述主线、主角目标、主要冲突 | 70% 复述与人工标注一致 |
| 原文溯源 | 用户点击任一判断，能跳到正确场景并看到高亮 | 95% 成功 |
| 改写定位 | 用户点击改写候选（位于「行动 · 编剧」段级列表），Agent 收到完整 brief（题材 / 综合分 / 维度评分 / 原文 quote / 维度差异化目标），给出非表面润色方案。详见 docs/10-rewrite-agent.md §3-4 | 人工评审 ≥ 3/5 |

## 6. 失败分级

| 等级 | 例子 | 处理 |
|---|---|---|
| P0 | 溯源跳错剧本 / 跳错场 / 关键场显示单句 quote | 阻断发布 |
| P1 | 主角漏掉 / 关键反转漏掉 / recommendation 明显反向 | 修 prompt 或 chain |
| P2 | 关系类型不准 / 评分理由不够自然 | 进入迭代池 |
| P3 | 文案不够漂亮 / 图布局拥挤 | UI 优化 |

## 7. 回归流程

1. 每次改 `script_report_service` / `*_chain.py` 后，跑 3 份固定剧本。
2. 保存 `report_json` 快照到 `eval/reports/<script_slug>.json`。
3. 自动校验 schema、证据可溯源率、关键场摘要有效率。
4. 人工抽查 coverage、beat、character、rewrite 四类输出。
5. P0/P1 失败不进入演示环境。

## 8. 当前边界

| 边界 | 行为 |
|---|---|
| 人工标注样本小 | 只作为工程回归，不声称统计显著 |
| 剧本格式极差 | 先看 segmenter 输出；解析错误不归因给报告 chain |
| 价值判断有主观性 | 用「是否有原文依据 + 是否帮助决策」代替单一正确答案 |
| 改写质量难自动化 | 自动只评定位，文本质量走人工 1-5 分 |
