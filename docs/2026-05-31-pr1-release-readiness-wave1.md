# PR1 — Release Readiness Wave 1: Correctness & Provenance

**目标**：去掉「沉默成功」陷阱、把 LLM chain 的真实状态向用户/BI 透明，并加上
并发 / DB 状态的最小可上线保护。

**范围**：12 个 W1.x 改动 + 1 个 alembic migration + 1 个前端降级提示条组件。
对应原 system audit 中的 8 大 critical issue 全部落地。

> 政策：「禁止 silent success：失败必须显式标记 source=rule_fallback / degraded，
> 前端必须能看到「降级提示」（如「人物关系部分由规则补全」），不再骗用户。」

---

## W1.1 — `script_llm_segmenter` trim 后零丢失检查失效

**位置**: `backend/app/service/script_tools/script_llm_segmenter.py`

**根因**：
旧实现先用累积 `seen_paras` 通过零丢失检查，再 `out = out[:30]` 截断；被截
掉的场对应段落 silently 丢失，但 `seen_paras` 仍包含它们 → 零丢失检查被骗过
→ 下游 LLM 拿到不完整剧本。

**修复**：
- trim 前直接 reject，进入 `single_scene` fallback 路径（保证零丢失契约）
- trim 之后**用最终 `out` 重新计算 `final_covered` 与 `body_non_empty` 比对**，
  双重防御未来重新引入 trim 逻辑时复发

## W1.2 — 新增 `ChainResult[T]` 统一契约

**位置**: `backend/app/service/script_tools/chain_result.py` (NEW)

每个 chain 的产出统一成 `(data, status, source, fallback_reasons,
partial_failure_fields)`，约定：

| status | source 必须 | 含义 |
|--------|-------------|------|
| ok | llm | 全 LLM 信号，内容可信 |
| degraded | llm / hybrid | 部分规则补 / 部分字段缺失（可用，需提示） |
| failed | rule_fallback | LLM 整段失败，data 是规则降级产物 |

提供 `aggregate_overall_status()` 把多个 chain 状态聚合到报告整体 status。

## W1.3 — 全链路 provenance 透传到 `report.meta.chain_status`

**位置**: `backend/app/service/script_report_service.py`

`generate_report` 收集每个 chain 的 provenance：

- 失败：`_optional_chain` / `_safe_unwrap` 自动写 `status=failed,source=rule_fallback`
- 成功 + degraded：根据 chain 自身 `source` / `fallback_reasons` 写
- 成功 + ok：明确写入（**禁止 silent success**）

最终序列化到 `report_payload.meta.chain_status` + `meta.overall_status`，前端据此
渲染降级提示条。

**对接 chain 列表**：reward_extractor / beat_chain / character_graph_chain /
motivation_chain / bios / compliance_chain / coverage_chain。

## W1.4 — 前端报告级降级提示条

**位置**: `RavenWeb/.../component/scriptlens-report-rail.tsx` + `.module.scss`

新组件 `ReportProvenanceBanner`：
- `overall_status="ok"` → 不渲染
- `overall_status="degraded"` 且有 `chain_status` entry → 黄色 Alert
- 任一 chain `failed` → 升级为橙色 + 建议重新诊断

支持展开查看每个 chain 的 status / source / fallback_reasons。

工业对照：GitHub degraded badge / DataDog incident banner / Linear API throttled
banner。

## W1.5 — `scripts.status` 拆 ingest / analysis 两条独立生命线

**位置**: `backend/app/alembic/versions/09_split_script_status_and_scoring_run_state.py`

旧实现：`scripts.status` 同时表达「上传/解析」+「报告分析」。分析失败时把
status 翻成 `failed`，**覆盖**了 ingest 成功后的 `ready`，前端误显示「上传失败」。

新策略：
- `scripts.status` 严格只表达 ingest 生命周期（pending → ready / failed）
- 新增 `scripts.last_analysis_status`（running | done | failed | null）
- 分析失败**不再翻 scripts.status**，只写 `failure_reason='analysis_failed:...'`

DDL（alembic 09）：
- `ADD COLUMN last_analysis_status TEXT NULL` + CHECK 约束
- `ADD CONSTRAINT scoring_runs_status_check` 限定 `('running','done','failed')`

## W1.6 — `scoring_runs` 状态机 `running → done/failed`

**位置**: `script_report_service.py`

旧实现：scoring_runs 只在成功路径 INSERT。失败时无任何记录，BI 完全看不到。

新策略：
- `generate_report` 入口立刻 INSERT `status='running'`（带 `run_id` UUID）
- 持久化阶段 ON CONFLICT (id) DO UPDATE 改为 `status='done'`
- 失败路径 `_mark_scoring_run_failed` UPDATE 为 `status='failed'` + `error` 写入

dashboard 能看到「正在分析」/「上次失败」徽章。

## W1.7 — Per-script DB advisory lock

**位置**: `script_report_service.py`（`_try_acquire_script_lock` /
`_release_script_lock`）

防止同一 script_id 被并发 `generate_report`（reanalyze 重入、双开 worker、用户
连点重新诊断）。

- key = `(8101 << 32) | crc32(script_id)`（命名空间 + 哈希）
- `pg_try_advisory_lock(key)` 拿锁；session-scope 自动释放
- 长 connection 存进 module 级 dict `_active_lock_connections`
- 流水线 `finally:` 块显式 unlock + close

旧实现允许两个 generate_report 同时跑 → `persist_entities` 盲删旧
`character_entities` → CASCADE 误清 bios / relationships → id-space 漂移。
有了 advisory lock，这条 race 关掉。

## W1.8 — `gather(return_exceptions=True)` + compliance best-effort

**位置**: `script_report_service.py`

旧实现：`asyncio.gather(beat, graph, motivation, bios)` 默认
`return_exceptions=False`——任一 chain 抛 → sibling task 全 cancel → 整份报告失败。
compliance 也是直接 `await screen_compliance(...)`，单点失败拖垮报告。

新策略：
- gather 改为 `return_exceptions=True` + `_safe_unwrap` 把异常落到 `chain_status`
- compliance 包到 `_optional_chain`，失败用 `ComplianceResult.empty()` 占位
  （status=insufficient，不再让一个 LLM 抖动拖垮整报告）

## W1.9 — `BeatSheet.to_dict()` 包含 source / fallback_reasons

**位置**: `script_tools/beat_chain.py`

旧 `to_dict` 只输出 `acts`，前端永远显示绿灯——即便是规则补的也无感知。

新策略：
- `BeatSheet` dataclass 新增 `rule_replaced_beat_count` 字段
- `to_dict()` 输出 `source` / `fallback_reasons` / `rule_replaced_beat_count`
- 规则替换 LLM 低质量 summary 的 beat 数 → 累加到 `rule_replaced_summary_count`
- act 补位（LLM 漏掉某幕） → 累加到 `fallback_reasons`

任一 > 0 → `source='hybrid'`，触发 W1.3 提示链路。

## W1.10 — `character_graph` hard bridge `type=unknown` + `is_inferred`

**位置**: `script_tools/character_graph_chain.py`

旧实现：top-N 子图无跨分量共现时，硬兜底 bridge edge 用 `type="ally"` /
`polarity="mixed"` 假装是真关系，欺骗用户。

新策略：
- `CharacterEdge` 加 `is_inferred: bool = False` 字段
- 硬兜底 bridge edge 改为 `type="unknown"` + `polarity="unknown"` + `is_inferred=True`
- `CharacterGraph` 加 `enrichment_status` (ok / degraded / failed) + `enrichment_failed_reasons`
- enrichment LLM 失败时返回基线图但显式标 `failed`（旧实现 silent 返回基线）

前端可对 `is_inferred=true` 的 edge 渲染虚线 + tooltip「无共现证据，仅图连通性保证」。

## W1.11 — Motivation filter 失败 top-K + partial_failure

**位置**: `script_tools/motivation_chain.py`

旧实现：`_filter_real_decisions` LLM 失败时 `return candidates`——把全部关键词
召回都当真决策，noise 决策（如「我走了」日常吵架）直接污染动机分。

新策略：
- 取 top-K（默认 8），其余 candidate 舍弃
- 显式上报 `filter_degraded=True` + `filter_degraded_reason`
- `MotivationResult` 加 `partial_failure` / `judged_count` / `attempted_count`
- 上层 `chain_status` 据此标 `degraded`，前端能透出「决策筛选已降级」

## W1.12 — Coverage strengths / concerns 硬校验 + 无 url comparable 不展示

**位置**: `script_tools/coverage_chain.py`

旧实现：
- LLM 偶尔少给 1 条 strengths/concerns 或 analysis 字段缺失 → `_points` 静默跳过 → 前端显示「亮点·2」缺一格，用户不知道是 LLM 失误
- comparable_titles 搜索失败时返回 `platform="fallback"` 占位条，前端当真链接渲染

新策略：
- `CoverageCard` 加 `source` / `fallback_reasons` / `strengths_rule_filled_count` / `concerns_rule_filled_count`
- LLM 不足 3 条 → 记录到 `fallback_reasons=[strengths_only_X_of_3]`
- 搜索失败 → 不再返回占位 chip，记录 `comparable_titles_search_failed`
- 任一 > 0 → `source='hybrid'`

---

## 验证清单（手动跑前必看）

1. **Alembic migration**

   ```bash
   cd ScriptLens/backend
   alembic upgrade head
   ```

   预期：`09_split_status_state` 应用成功。
   注意：revision id 必须 ≤ 32 字符（`alembic_version.version_num` 限长 32）。

2. **冷启动一次新分析**

   - 上传新剧本 → 看 dashboard `last_analysis_status` 从 NULL → running
   - 等流水线完成 → `last_analysis_status='done'`
   - `scoring_runs` 表对应 run_id `status='done'`

3. **并发 reanalyze 测试**

   连点两次「重新诊断」，预期：第二次拿不到 advisory lock，立刻报错
   `script_id=... 正在被其他分析任务处理`，不再产生并发 race。

4. **故意失败 chain**

   把 `LlmCaller.call_json` 临时改成总是抛 `ScoreLLMError`，触发整链失败：

   - 报告仍能出（rule_fallback 路径）
   - `meta.chain_status` 显示对应 chain `status=failed,source=rule_fallback`
   - `meta.overall_status='degraded'`
   - 前端顶部出现橙色降级提示条，展开能看到每条 reason
   - `scripts.status` 保持 `ready`
   - `scripts.last_analysis_status='failed'`
   - `scoring_runs` 对应 run_id `status='failed'` + `error` 字段有值

5. **frontend lint**

   预先存在的 14 个 docStudio.ts unknown cast lint 是 pre-existing，与本 PR 无关。

---

## 验证结果（2026-05-31 已跑）

| 项 | 状态 | 证据 |
|----|------|------|
| `chain_result.py` 合约 (ok/degraded/failed) | ✅ | static smoke |
| Pydantic `ReportPayload.meta` / `ViewResponse.meta` | ✅ | model_dump_json 含 chain_status |
| Alembic `09_split_status_state` (重命名前太长) | ✅ | `alembic current` 至 head |
| `scripts.last_analysis_status` + CHECK 约束 | ✅ | psql 查 pg_constraint |
| `scoring_runs.status` CHECK 约束 | ✅ | 同上 |
| `_try_acquire_script_lock` 真锁 + 拒重入 | ✅ | smoke：第二次返回 False |
| `_insert_scoring_run_running` → `_mark_scoring_run_failed` 状态机 | ✅ | smoke + 真实端到端 |
| `_mark_analysis_status` 写 running/done/failed + CHECK 拦截非法值 | ✅ | smoke |
| `_build_report_payload` 把 chain_status 注入 `meta` | ✅ | smoke + DB query 落库 |
| 端到端 `generate_report` 跑完 32 场剧本（257s） | ✅ | 7 个 chain 全在 `meta.chain_status` |
| `beat_chain` LLM 失败 → `degraded` + `source=rule_fallback` + `fallback_reasons=['llm_error:ScoreLLMError']` | ✅ | DB 行：见 W1.9 字段 |
| `meta.overall_status='degraded'` 聚合正确 | ✅ | DB query |
| `scripts.last_analysis_status='done'` + `status='ready'` 不被覆盖 | ✅ | DB query |
| 并发 reanalyze 被 advisory lock 拒（PARALLEL_REJECTED_OK） | ✅ | 真实双进程并发 |
| advisory lock 任务结束后自动释放（无 lock 泄漏） | ✅ | `pg_locks` 清空 |

> 已观察到的真实降级路径：32 场剧本跑一次时 beat_chain 抛 `ScoreLLMError`，
> 流水线整体继续，最终报告含 `overall_status=degraded`，beat_chain 走 rule_fallback。
> 这正是 PR1 想要保证的「不再骗用户」效果。

---

## 不在本 PR 范围（已记 plan）

- PR2 (Wave 2)：Instructor schema、retry/backoff、tier 分级、observability、metrics、opt-in cache
- PR3 (Wave 5)：删除 evaluation_chain / improvement_action / tag_pipeline 等 deprecated 模块
