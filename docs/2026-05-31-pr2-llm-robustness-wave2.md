# PR2 — Release Readiness Wave 2: LLM Robustness & Observability

**目标**：在 PR1 「provenance / 状态机 / 并发锁」之上，把 LLM 调用层做扎实：
同模型限流/抖动可恢复、token/latency 可观测、可选 cache 与 schema 校验能力就位。

**范围**：6 个 W2.x 改动，全部集中在 `script_tools/llm_caller.py`，向后兼容
不破坏现有 21 个调用点。

> 策略：与其引入 instructor / tenacity 新依赖，不如把这些能力**内嵌进 LlmCaller**，
> 让 provider fallback / capability matrix / token budget / retry / cache /
> observability 共享同一份控制流，避免「retry 撞 fallback 互相干扰」的隐患。

---

## W2.1 — Pydantic schema validation + 1 次反馈重试（Instructor 模式）

**位置**: `LlmCaller._validate_or_raise`、`_validate_with_repair`、`call_json(validate_with=)`

**为什么不直接装 `instructor` 包**：

1. 已有完整 `_call_provider_with_fallback` + `_fallback_probe` 路径，引入 instructor
   会让 retry 层数从 1 层（同 model） + 1 层（切 model） + 1 层（切 provider） 变成
   1 层（instructor）× 上述 3 层 = 12 次潜在 LLM 调用，成本难控。
2. instructor 把 OpenAI client 包成 `instructor.from_openai(client)`，会拦截
   我们的 capability matrix。要么改动我们的 client 创建路径，要么 patch instructor。
3. 评分链路的大 chain（beat / coverage / character_graph）**故意**做了 partial
   recovery（act3 缺失 → rule 兜底单独 act3），整体 schema reject 会破坏这种容错。

**做的事**：
- `call_json(validate_with=BaseModel | None)` 参数新增（默认 None，行为不变）
- 命中 ValidationError 时：把 `model_json_schema()` + 错误信息 + 原 prompt 注入
  repair_prompt，再调一次 LLM，trace_id 加 `.repair` 后缀
- 仍失败 → 抛 `ScoreLLMSchemaError`（新异常类型，与 ScoreLLMError 同层）

**给现有 chain 的策略**：
- 不强加给 beat / coverage / character_graph（保留它们的 partial recovery 设计）
- 能力就位，未来如 `risk_screener` / `motivation_chain` 这种「输出窄、必须严格」
  的 chain 可以选择性开启

## W2.2 — 同 model 429 / timeout / 5xx 指数退避

**位置**: `LlmCaller._call_with_provider`, `_backoff_delay`

**旧行为**: 任何 `RateLimitError` / `APITimeoutError` / 5xx → 立刻向上抛 →
`_call_json_internal` 切下一个 provider/model。结果：qwen-max 偶发限流，整个评分链
立刻退化到 rule_fallback（即使等 2 秒就能恢复）。

**新行为**:
- 同 model 退避重试 `_SAME_MODEL_RETRY_MAX=3` 次（首发 + 2 次退避）
- 指数：`base=1.0s * 2^(n-1)`，cap `8.0s`，±25% 抖动避免雪崩
- 实际间隔约：1.0s → 2.0s → 4.0s（含 jitter 上下浮动）
- 仍失败才向上抛，由外层切 provider/model

**仅退避以下场景**（避免无效重试）：
- `RateLimitError`、`APITimeoutError`、`httpx.TimeoutException`
- `APIError` with `status_code in [500, 599]`
- 4xx 立刻向上抛（参数错 / 模型不存在 / 鉴权失败 不会因等待恢复）

## W2.3 — 任务 tier 分级体系（plumbing only）

**位置**: `ModelTier` (已存在)、`call_json(chain_name=...)`

**与原 plan 的差异**：
原 plan 提「把 segmenter / motivation filter 路由到 MINI 省 80% 成本」。但
21 个调用点全部显式写 `tier=ModelTier.PRIMARY`，且代码注释明确写「全部走 primary：
用户要求所有分析链路用强模型」。**尊重该决策**，不强行降档。

**本 PR 做的事**：
- 保留 `ModelTier.MINI` 路由能力（已存在）
- 给 `call_json(chain_name=...)` 加 chain 名传参，未来如要按 chain 切档可在配置层完成
- 不动任何调用点

## W2.4 — LLMResponse 加 trace_id / prompt_hash / usage / attempts / cache_hit

**位置**: `LLMResponse` dataclass、`_call_with_provider`、`_hash_prompt`、`_extract_usage`

字段新增（**全部带默认值，向后兼容**）：

| 字段 | 类型 | 来源 |
|------|------|------|
| `trace_id` | str | 调用方传入；不传则本层生成 `uuid4().hex` |
| `prompt_hash` | str | `sha256({prompt, system, tier})` 前 16 位 |
| `usage` | dict\|None | `{prompt_tokens, completion_tokens, total_tokens}` |
| `attempts` | int | 同 model 实际重试次数（W2.2 配套）|
| `cache_hit` | bool | 是否命中 opt-in cache（W2.6 配套）|

调用方现在可以打全链路 log：

```python
resp = await caller.call_json(prompt, chain_name="beat_chain")
logger.info(
    "beat_chain trace_id=%s prompt_hash=%s elapsed=%dms attempts=%d usage=%s",
    resp.trace_id, resp.prompt_hash, resp.elapsed_ms, resp.attempts, resp.usage,
)
```

## W2.5 — Prometheus / LLM metrics（复用 agent_runtime sink）

**位置**: `_call_with_provider` 末尾调 `_record_llm_usage`

**关键发现**: `agent_runtime/metrics.py` 已经有完整的 LLM metrics sink：
- `record_llm_usage(provider, model, success, duration, prompt/completion/total_tokens, cost)`
- `format_prometheus_metrics()` 输出 `doc_studio_llm_*` Prometheus 文本

**做的事**：
- LlmCaller 调用成功 / JSON 失败 / 内容为空 三个分支都上报 metrics
- model 名 `+"::cache"` 后缀区分 cache_hit 路径（不污染主 model 维度）
- import 用 `try/except` 包住，单元测试 / 子进程无 agent_runtime 时降级为 no-op

新可见指标（无需新代码，自动通过现有 `format_prometheus_metrics` 暴露）：
- `doc_studio_llm_requests_total{provider, model, status}`
- `doc_studio_llm_duration_seconds_total{provider, model}`
- `doc_studio_llm_tokens_total{provider, model, type=prompt|completion|total}`

## W2.6 — call_json opt-in cache

**位置**: `call_json(use_cache=False, cache_ttl_s=None)`、`_try_cache_get/_try_cache_put`

**为什么 opt-in（默认关闭）**：
评分链路绝大多数 chain 都是「读相同剧本 → 重新评分」的场景，**不应该**返回老结果——
用户改了原文重新分析，缓存反而是 bug。所以默认不缓存。

**显式开启的场景**（未来）：
- `risk_screener.quote_confirm`：同一句话 quote → 同一判定，可缓存
- `bio_writer` 单角色再生：同 prompt 同 system 完全可复用

**实现要点**：
- 复用 `scriptlens.llm_cache` 表（不新建）
- cache key = `_hash_prompt(prompt, system, tier)`（**不含 model**，让 fallback 切换后
  仍能命中——「我只想要正确 JSON」语义）
- TTL 暂未真正实现（需要表加 `last_hit_at` 字段），代码里留接口位
- 命中 cache 仍上报 metrics（model 名加 `::cache` 后缀，方便观察命中率）
- 命中 cache 时若有 `validate_with` 也走校验（防 schema 漂移）

---

## 验证（2026-05-31 已跑）

| 项 | 状态 |
|----|------|
| `LlmCaller` import 不破坏（含 deterministic / runtime / cache） | ✅ |
| `LLMResponse` 新字段默认值 | ✅ |
| `_hash_prompt` 稳定 + tier 区分 | ✅ |
| `_backoff_delay` 指数 + jitter 边界 | ✅ |
| `_extract_usage` 处理 None / SDK 对象 / dict 三种形态 | ✅ |
| `_validate_or_raise` cache_hit 路径同步校验 | ✅ |
| `_validate_with_repair` 1 次失败 + 1 次 repair 成功 | ✅ |
| `record_llm_usage` 写入 `agent_runtime` sink + `collect_metrics_summary` 可读 | ✅ |
| `ModelTier.resolve_candidates(MINI)` 仍返回有效列表 | ✅ |

后续 PR1 端到端复跑（含 W2.2 退避）观察项：
- 同 model 限流时报告生成时间略增（每次 1-4s），但 chain status 仍为 `ok` 而非
  `degraded`——这就是 W2.2 的价值
- `agent_runtime.metrics` 的 `/metrics` endpoint 可看到 `doc_studio_llm_*` 包括
  评分链路的指标（之前只有 ReAct agent）

---

## 未启用 / 未做（已记 plan）

- **W2.1 schema 强校验未应用到 beat / coverage / graph**：能力就位，决策见上。
  未来若想给 `risk_screener.quote_confirm` 接入只需在调用点加
  `validate_with=RiskConfirmSchema`。
- **W2.6 TTL 真实生效**：需要 `LlmCache.get` 暴露 `last_hit_at`。本 PR 留接口位。
- **W2.5 暴露端点**：评分链路目前没单独的 HTTP `/metrics`；复用 doc_studio agent
  endpoint 即可看到指标。如要分拆，下一个 PR 加路由即可。

---

## 与 PR1 / PR3 的关系

| PR | 主题 | 状态 |
|----|------|------|
| PR1 | Correctness & Provenance | ✅ merged |
| **PR2** | **LLM Robustness & Observability** | **本 PR** |
| PR3 | Wave 5 — 删 deprecated（evaluation/improvement/tag_pipeline） | 下一个 |
