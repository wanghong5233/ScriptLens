# Playbook Examples · 反例 → 正例

## 反例：散文化技术博客 + 项目绑定 + 辩证过程

```markdown
# 关于 LLM 路由的一些思考

最近在做 ScriptLens 的时候，发现某个 LLM provider 经常超时，我们一开始用的是简单的
模型级 fallback，比如 model-a 失败就降级到 model-b，但这样有个问题——provider
整个不可达的时候，每个模型都要等 60s timeout，加起来就是几分钟。

我们考虑了几种方案：
1. 模型级熔断：每个模型独立计数。优点是粒度细，缺点是 provider 全断时仍然慢。
2. Provider 级熔断：整个 provider 算一个单位。这个比较 stripe 的设计……

最后我们选择了方案 2，效果不错……
```

问题：
- 项目绑定（ScriptLens / 具体 provider / 具体 model 值）
- 散文化讲故事（最近 / 一开始 / 后来）
- 辩证过程（考虑了 A / B / 最后选 C）
- 业界对比铺垫（比较 stripe……）
- 时效语（最近）

## 正例：抽象原则 + 反模式表 + 第一性原理

```markdown
# 08 · Provider 级熔断 / Provider-Level Circuit Breaker

## 现状陈述

LLM 调用 fallback 必须按 provider 而非按 model 计数。同一 provider 不可达时，
该 provider 下所有 model 在当前请求内必须立即跳过，不再逐个尝试。

## 反模式 vs 正例

| 维度 | 反模式 | 正例 |
|---|---|---|
| 计数粒度 | 按 model 计 timeout 次数 | 按 provider 计连续不可达次数 |
| 失败传染 | 一个 model 失败，下一个仍发起调用 | provider 标黑后，本请求内全部跳过 |
| 探测窗口 | 每次请求都重新探测 | 失败窗口内复用判定 |
| 可观测 | 只记录最终 fallback 用了哪个 model | 记录跳过的 provider 列表 |
| 跨请求 | 状态不持久 | 内存级 TTL + 重试探活 |

## 第一性原理

| 维度 | 分析 | 结论 |
|---|---|---|
| 异质性 | 不同 provider 的失败模式不同（网络/限流/认证）| 失败语义必须按 provider 分类 |
| 故障域 | 一个 provider 的网络问题会同时影响其所有 model | 失败传染必须在 provider 层切断 |
| 可逆性 | 网络问题往往秒级恢复 | 必须有 TTL 自动恢复 |
| 可观测 | 排障时需要回答"哪个 provider 当时被跳过" | 响应/日志必须带 `skipped_providers` |

## 触发抽象的信号

1. fallback 链路里出现"同一 provider 全部 model 挨个超时"现象
2. 单次请求总耗时 = `N × timeout`，N = 该 provider 的 model 数
3. 排障日志只能看到最后一个 model 的失败，看不到前面被跳过的
4. 出现 `if model_name.startswith(...)` 之类的硬编码 provider 判别

## 自检清单

- [ ] fallback 计数是按 provider 还是按 model？按 model → 改
- [ ] provider 标黑有 TTL 吗？无 → 加
- [ ] 响应里能看到本次请求被跳过的 provider 吗？看不到 → 加审计字段

## 反向链接

- 兜底审计字段 → [04-loud-failure](./04-loud-failure.md)
- 参数治理 → [01-parameter-governance](./01-parameter-governance.md)
```

## 判别口诀实操

写完 playbook 草稿后，做一次"抽象替换"测试：

把所有项目业务名词替换为抽象概念：
- `EvidenceRef` → 证据引用
- `script_id` → 业务对象 ID
- `pypdf` → 文件解析库
- `DashScope` → LLM provider
- `NEXT_PUBLIC_API_BASE_URL` → 前端公开 API 地址

替换后通读：文章如果还成立 → 合格；如果立刻散架 → 这是 pitfall 或 architecture 的料，不是 playbook。
