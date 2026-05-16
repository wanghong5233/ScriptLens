---
name: llm-cost-optimizer
description: Optimize LLM/Agent API cost without losing quality. Use when launching AI features, choosing models, prompt/context grows large, token spend rises, max_tokens/cache/model routing is unclear, or user asks to reduce LLM cost. Do NOT use for quality eval alone or RAG architecture alone.
---

# LLM Cost Optimizer

## 一句话准则

**LLM 成本不是账单问题，而是架构问题：先按 feature 计量，再按任务复杂度路由，最后用缓存和输出上限收口。**

## 先分类

| 模式 | 什么时候用 | 第一产物 |
|---|---|---|
| Cost Audit | 已有花费但不知道钱去哪了 | per-feature cost schema |
| Optimize Existing | 已知高成本路径 | top 3 ROI fixes |
| New Feature Design | 准备上线 Agent/LLM 功能 | cost guardrails before launch |

没有 token/cost 日志时，不做 prompt compression；先补计量。

## 最小成本日志

每次 LLM/provider 调用记录：

| 字段 | 说明 |
|---|---|
| `feature` | ask / summarize / deep_research / parser_enrich |
| `provider` / `model` | 供应商与模型 |
| `input_tokens` / `output_tokens` | 原始 token |
| `cache_hit` | prompt/cache 是否命中 |
| `latency_ms` | 端到端和 provider 分段 |
| `cost_usd` | 按当时价格表计算 |
| `route_reason` | 为什么选这个模型 |
| `policy_version` | 成本策略版本 |

## 优化顺序

1. **Model routing**：按任务复杂度选最便宜可胜任模型；禁止默认全打大模型。
2. **Prompt caching**：系统提示词、静态 few-shot、长文档上下文优先缓存。
3. **Output control**：按 endpoint 设置 `max_tokens`，不要全局上限。
4. **Context pruning**：删重复上下文、过期规则、无效 examples。
5. **Semantic cache**：对重复问法高的查询做相似度缓存。
6. **Async / batch**：非实时任务进队列，降低峰值成本和失败率。

## Proactive Flags

| 信号 | 动作 |
|---|---|
| 所有请求都打同一个大模型 | 设计 model route |
| system prompt >2000 tokens 且每次发送 | 加缓存或拆静态上下文 |
| endpoint 没有 `max_tokens` | 按 p95 output 设置上限 |
| 没有 per-feature spend | 先加日志，不优化 |
| free/user demo 与 paid path 同模型 | 按用户层级 / 功能层级路由 |
| 重试导致 token 放大 | 加 provider circuit breaker 与 retry budget |

## 质量保护

成本优化必须有质量护栏：

- 每个 route 变更跑 golden set。
- 小模型替换大模型时记录 before/after pass rate。
- 缓存命中必须带 `cache_hit=true`，并能强制 bypass。
- prompt compression 只删 filler，不删 task-critical instruction。
- 成本下降但失败重试上升，视为失败。

## 输出格式

```markdown
## Baseline
## Cost Drivers
## Optimization Plan
## Quality Guardrails
## Expected Savings
## Rollout / Rollback
```

## 链接

- 上游来源与改写说明：`references/upstream.md`
- 质量验证：`llm-observability-and-evals`
- Provider 级故障调试：`agent-debugging`
