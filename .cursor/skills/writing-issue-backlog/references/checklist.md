# Issue Backlog Checklist

## 单条 Issue 提交前

### 类型与状态

- [ ] 已声明 `type`: `bug` / `improvement` / `validation`
- [ ] 已声明 `status`: `triaging` / `investigating` / `planned` / `blocked`（仅这 4 值）
- [ ] 已声明 `priority`: P0 / P1 / P2 / P3
- [ ] 优先级判定基于"用户可感知影响"而非"工作量"

### Bug 类必填字段（顺序固定）

- [ ] `Symptom`：1-2 句现象，不解释
- [ ] `Repro`：他人可原样执行的步骤 + 环境
- [ ] `Observed Evidence`：日志关键词 / payload / 状态码原文，2-4 行
- [ ] `Scope`：影响范围 + 版本
- [ ] `Impact`：业务/用户后果，量化优先
- [ ] `Hypotheses`：编号假设，互斥可证伪
- [ ] `Open Questions`：每条可被一次实验关闭
- [ ] `Root Cause`：仅在假设收敛后写
- [ ] `DoD`：每条可观测、可验证
- [ ] `Next Step`：仅在 Root Cause 写完后写实现方案

### Improvement 类必填字段

- [ ] `Current Behavior` / `Limitation` / `Trigger Condition` / `Options Considered` / `DoD` / `Next Step`
- [ ] `Trigger Condition` 未达成 → `status` 保持 `triaging` 或 `blocked`

### Validation 类必填字段

- [ ] `Subject` / `Test Plan` / `Pass Criteria` / `Environment` / `Result`

### 关键硬约束

- [ ] Bug 类 `Root Cause` 为空时，`Next Step` 仅写"补证据 / 召集决策"
- [ ] 没有"默认改成 X / 自动 Y / 走 Z 路径"未经评审的补丁式表达
- [ ] 没有"后续会处理 / TBD / 待定"占位语言
- [ ] 没有 30+ 行日志全文（留关键 3 行 + 链接）
- [ ] 单段落 < 3 行
- [ ] 单条 Issue 总长度 < 60 行（超出 → 拆 ADR / RFC）

## 文件级别（全局）

- [ ] 同一根因只保留一个 Issue ID（合并重复）
- [ ] ID 未复用已关闭的号段
- [ ] 没有"已上线 / 已修复"残留条目
- [ ] 索引表的"当前阶段下一步"不含最终方案描述
- [ ] 所有 `blocked` ≥ 60 天条目已复检

## 演进规则触发

- [ ] 新报告进 `triaging`，30 天补不齐 Repro/Evidence → 关闭
- [ ] 同类问题在 ≥2 项目出现 → 上抽象到 engineering-playbook
- [ ] 季度回顾走完
