---
name: llm-observability-and-evals
description: Design observability and eval contracts for production LLM/Agent systems. Use when adding ask/retrieval/tool/model flows, debugging quality regressions, defining trace/log/metric fields, building golden sets, or proving an Agent feature works. Do NOT use for cost-only work.
---

# LLM Observability & Evals

## 一句话准则

**Agent 上线不是"能回答"就算完成，而是每次决策都能回放、每次质量变化都能被 eval 捕捉。日志是行为合同，eval 是质量合同。**

## 最小上线合同

任何 LLM/Agent 路径上线前至少具备：

| 类别 | 必备字段 / 产物 |
|---|---|
| Trace | `trace_id`, `conversation_id`, `attempt_id`, `job_id` |
| Decision | `route_reason`, `index_mode`, `tool_choice`, `model_route`, `policy_version` |
| Execution | retrieval/tool/model 每段 `latency_ms`, `status`, `error_class` |
| Quality | golden set、pass threshold、失败样例归档 |
| Cost | `input_tokens`, `output_tokens`, `model`, `provider`, `feature` |
| UX state | `accepted`, `running`, `failed`, `timed_out`, `completed` |

缺一类时，先补 contract，再谈优化。

## Observability 设计流程

1. **画链路**：用户输入 → route → retrieval/tool → model → persistence → UI。
2. **标决策点**：每个 `if/route/fallback/retry` 都要有 `reason`。
3. **标分段耗时**：routing / retrieval / tool / generation / postprocess 分开计。
4. **统一字段名**：同一概念只允许一个 schema 名。
5. **采样策略**：高频成功路径可采样，失败路径必须全量。
6. **隐私审计**：prompt 可摘要化，secret/token/cookie 不落日志。

## Eval 设计流程

| 阶段 | 做什么 |
|---|---|
| Golden Set | 从真实 bug、用户问题、边界样例沉淀 20+ 条 |
| Metrics | 分类/抽取用 exact/schema；摘要/问答用 rubric 或 LLM-as-judge |
| Baseline | 记录当前生产 prompt/model/retrieval 的分数 |
| Gate | prompt、retrieval、model route 改动必须跑 eval |
| Drift | 定期重跑，发现模型更新或数据变化导致的退化 |

## Agent 专属 Eval 维度

| 维度 | 问题 |
|---|---|
| Intent Routing | 是否按用户意图选择 RAG / direct context / tool |
| Retrieval Quality | 是否召回正确文档、正确片段、足够证据 |
| Tool Use | 是否选择正确工具、传参合法、处理错误 |
| Answer Grounding | 回答是否引用 evidence，而不是编造 |
| State Durability | 失败时用户输入和 attempt 状态是否可见 |
| Recovery UX | 失败是否可重试、错误是否可解释 |

## 反模式

| 反模式 | 风险 | 改法 |
|---|---|---|
| 只有最后答案，没有中间决策 | 无法解释为什么检索/调用工具 | 记录 decision + execution |
| 只靠人工点点看 | 回归不可重复 | golden set 进 CI |
| 日志字段各服务自定义 | 查询和 join 失败 | schema 进文档与测试 |
| 质量指标只看 thumbs up | 样本稀疏且滞后 | 线上反馈 + 离线 eval 双轨 |
| 失败只返回 500 | 用户和工程都无法恢复 | 结构化 `error_class` + retryable |

## 输出格式

```markdown
## Trace Contract
## Metrics
## Eval Set
## Pass Criteria
## Dashboards / Alerts
## Gaps Before Launch
```

## 链接

- 上游来源与改写说明：`references/upstream.md`
- 成本优化：`llm-cost-optimizer`
- Prompt 版本治理可从本 skill 拆出 `prompt-governance`
