---
name: tool-and-mcp-design
description: Design production-grade Agent tools and MCP servers with clear schemas, permissions, errors, idempotency, and evals. Use when building tool calls, MCP integrations, external API actions, parser/web/search/database tools, or asking how an Agent should safely use external services.
---

# Tool & MCP Design

## 一句话准则

**工具不是函数暴露给模型，而是给不可靠调用者设计的产品接口：schema 要窄、权限要明、错误要可恢复、结果要可验证。**

## 设计顺序

1. **Workflow first**：先写用户要完成的真实任务，再决定工具边界。
2. **Tool contract**：定义 name、description、input schema、output schema、side effects。
3. **Permission model**：标清 read/write/destructive/open-world。
4. **Error semantics**：错误必须告诉 Agent 能不能重试、怎么恢复。
5. **Idempotency**：写操作必须有 idempotency key 或重复保护。
6. **Eval questions**：至少 5 个真实任务验证工具是否可被 Agent 正确组合。

## Tool Contract Checklist

| 维度 | 要求 |
|---|---|
| Name | 动词 + 资源；同服务统一前缀，如 `script_search`, `segment_get` |
| Description | 说明何时用、何时不用；不要只复述函数名 |
| Input | 使用结构化 schema；字段有约束、枚举、默认值说明 |
| Output | 返回结构化数据；避免只给自然语言 |
| Error | `error_code`, `retryable`, `user_message`, `developer_hint` |
| Side Effect | 明确 read-only / write / destructive |
| Pagination | 列表必须支持 limit/cursor，不一次返回全量 |
| Secrets | token/key 只从 env/config 读取，不进 prompt 或 tool args |

## MCP / Tool 反模式

| 反模式 | 后果 | 改法 |
|---|---|---|
| 一个工具包住整个 API | Agent 无法发现正确操作 | 拆成资源级工具，保留 workflow helper |
| 返回大段 Markdown | 难以复用、难以判断成功 | structured content + short summary |
| 错误只抛字符串 | Agent 不知道重试还是停止 | 结构化 error semantics |
| 写操作无幂等 | 重试导致重复创建/扣费 | idempotency key + conflict response |
| 工具描述含糊 | 误调用或漏调用 | description 写 what + when + negative |
| destructive 默认可用 | 企业环境风险过高 | 显式确认或 deny-by-default |

## Agent Tool Eval

每个工具集至少准备：

1. **Happy path**：一次或多次 tool call 完成真实任务。
2. **Empty result**：无数据时 Agent 能解释，而不是编造。
3. **Recoverable error**：429/timeout/temporary 失败能重试或降级。
4. **Permission denied**：无权限时不继续猜测。
5. **Destructive guard**：删除/覆盖/支付类操作不会静默执行。

## 输出格式

```markdown
## User Workflow
## Tool List
## Input / Output Schemas
## Permission & Side Effects
## Error Semantics
## Eval Questions
## Security Gaps
```

## 链接

- 上游来源与改写说明：`references/upstream.md`
- 调试工具调用失败：`agent-debugging`
- 工具效果验证：`llm-observability-and-evals`
