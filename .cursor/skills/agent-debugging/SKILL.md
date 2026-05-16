---
name: agent-debugging
description: Systematically debug production Agent failures across UI, API, retrieval, tools, model providers, and async jobs. Use when tests fail, logs show timeout/429/503/parser/RAG/tool errors, behavior drifts from user intent, or user asks to 查日志/定位根因/不要猜. Do NOT use for documenting issues only.
---

# Agent Debugging

## 一句话准则

**先冻结证据，再缩小故障域。Agent bug 往往不是单点代码错，而是 intent → route → retrieval/tool → model → persistence → UI 的链路漂移。**

## 先停线

遇到异常时先做 5 件事：

1. 停止新增功能或扩大修改面。
2. 保存原始 evidence：用户输入、payload、日志、状态码、trace_id、环境变量版本。
3. 明确用户期望与系统实际行为的差异。
4. 只提出可证伪假设，不写补丁式方案。
5. 修复后补 regression guard：测试、日志字段、backlog DoD 三选一以上。

## Agent 故障域分层

| 层 | 典型信号 | 先看什么 |
|---|---|---|
| UI / 状态文案 | 显示"检索中"但不确定是否真检索 | 前端 payload、阶段事件、后端 route 决策 |
| API / Orchestrator | timeout、503、accepted 后无结果 | request_id、state machine、持久化时机 |
| Retrieval / RAG | 回答引用错、意外检索、空召回 | index_mode、retrieval plan、top_k、query rewrite |
| Tool / MCP | 工具调用失败、权限错误、返回不可用 | tool schema、error code、idempotency、permission |
| Provider / Model | 429、5xx、长时间 generation | provider、model、retry、circuit breaker、latency |
| Async Job | 上传成功但后台失败 | job status、parser trace、队列、fallback path |
| Persistence | 输入丢失、重复 DOI、NUL 字符 | transaction boundary、unique constraint、sanitize |

## 调试流程

1. **Reproduce**：写出最小复现步骤；不能复现就标 `triaging`，不要修。
2. **Trace**：串起 `trace_id/request_id/conversation_id/job_id`，确认断点在哪一层。
3. **Compare**：对照"期望 contract"与"实际 execution"，分清文案误导还是真执行错误。
4. **Hypothesize**：列 2-4 个互斥假设；每个假设配一个实验关闭它。
5. **Fix Root Cause**：只改根因所在层；不要在上游 UI 做下游契约的补丁。
6. **Guard**：补测试、结构化日志、状态字段或 backlog DoD。

## 常见反模式

| 反模式 | 为什么危险 | 改法 |
|---|---|---|
| 看到日志就直接改代码 | 可能修的是表象 | 先定位 fault boundary |
| 用启发式绕过用户问题 | 新默认行为会污染产品契约 | 先写 Hypotheses / Open Questions |
| 只看最后一个错误 | Agent 链路中最后错误常是连锁反应 | 向前追第一个 contract break |
| 本地临时变量当修复 | demo 止血会混入正式逻辑 | 标注 Phase 0，并写结构化修复 |
| 无 trace_id 调试 | 无法复盘跨服务路径 | 先补可观测字段 |

## 输出格式

调试结论用这个结构：

```markdown
## Symptom
## Repro
## Evidence
## Fault Boundary
## Hypotheses
## Root Cause
## Fix
## Regression Guard
```

## 链接

- 上游来源与改写说明：`references/upstream.md`
- 问题记录落库：`writing-issue-backlog`
- 可观测字段设计：`llm-observability-and-evals`
