# ADR: Operation ID 显式来源协议

## 背景

ScriptLens 当前存在两类操作快照来源：

- `scriptlens.script_operations`（DB，`UUID` 主键）
- `.agent_history/operations`（文件快照，`timestamp_trace` 风格 ID）

历史实现通过“尝试按 UUID 查询，失败再走文件快照”判断来源。该方式可工作，但来源判定隐式，长期会造成维护复杂度上升。

## 决策

对外统一使用显式来源协议：

- `db:<uuid>`
- `history:<operation_id>`

适用范围：

- `OperationSummary.operation_id`
- chat/agent 响应里的 `operation_id`
- `/operations/{operation_id}/snapshot`
- `/operations/{operation_id}/revert`

## 设计原则（第一性原理）

- 用户链路只应有一个稳定标识语义，不应依赖“猜测字符串格式”。
- 来源分发只能在边界层做一次，业务层不散落分支。
- 错误应显式暴露，避免 silent fallback。
- 保留 legacy 输入解析仅作为迁移过渡，不新增新的隐式协议。

## 落地结果

- DB 操作记录列表统一返回 `db:<uuid>`。
- Agent 历史操作统一返回 `history:<operation_id>`。
- 快照读取先解析协议，再进入对应存储路径。
- 前端请求路径对 `operation_id` 做 URL 编码，避免保留字符导致路由歧义。
- operations 相关 API 错误统一返回 `detail={code,message}`，前端按 code 精准提示。
- script 高层 API（chat / feedback / view）同样采用 `detail={code,message}`，保持错误协议一致。
- script 读写/导出链路（get/delete/scenes/rewrite/reanalyze/export）也统一为同一错误协议。

## 后续收敛

- 已移除 legacy 无前缀解析逻辑，仅接受显式协议。
- 若未来将双存储收敛为单存储，此协议仍可平滑演进（保留 `db:` 或引入新来源前缀）。

## 关联文档

- `docs/playbook/12-error-codes.md`
