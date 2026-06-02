# PR3 — Release Readiness Wave 5: Deprecated Module Cleanup

**目标**：清掉 v1-mvp 已经不在主路径上的 deprecated 模块，让后续维护者不再迷路
（"这个文件还在用吗？" → 主仓库里只剩在用的）。

**策略**：删除前用 subagent 做精确引用审计，分两批：
- **PR3a (commit `c45fc72`)**：GREEN — 零生产 importer 的真死代码 + 它们的测试
- **PR3b (commit `6bdde8e`)**：YELLOW 块 — `tag_pipeline` 整棵子图 + 3 个 CLI + 14 个测试
- **W5.4 端到端验证**：PR1+PR2+PR3a+PR3b 累积下，跑一次真实 reanalyze 验证零回归

---

## 删除前精确审计（subagent 输出，未改任何代码）

判断标准：

| 状态 | 标准 |
|------|------|
| GREEN | 零 importer 或仅自己的测试文件 import → 可直接删 |
| YELLOW | 仅被另一个 deprecated 文件 import → 删除时配套删 |
| RED | 被 active 代码 import → 必须先重构，本 PR 不能删 |

审计对 `app/service/script_tools/` 与 `app/cli/` 全量做了 `^from .* import` /
`^import .*` 精确匹配，并在每个 active 入口文件（router/main app/script_report_service/
character_graph_chain/character_pipeline/dimension_scorer）反向核对。

**关键 RED 例外**：
- `pacing_aggregator.py` — 虽然历史 docstring 写「dead-code」，**实际 active**
  （`script_report_service` 在 L53/L1553 调 `aggregate_pacing_curve`），**绝不能删**。
- `models/improvement_action.py` — 因 `rewrite_chain` 仍有 raw SQL
  `FROM scriptlens.scoring_improvement_actions`，需先重构 rewrite_chain 才能删。

---

## PR3a — GREEN 删除（commit `c45fc72`）

`-1278` 行代码，`-7` modules + `-5` tests。

| 模块 | 删除理由 |
|------|---------|
| `evaluation_chain.py` | 零 importer |
| `improvement_action_generator.py` | 仅 1 个测试 import；主流程从不调 |
| `decision_aggregator.py` | 零 importer |
| `dimension_aggregator.py` | 仅 deprecated 兄弟（improvement_action_generator / decision_aggregator / genre_weights） + 测试 |
| `genre_weights.py` | 仅 decision_aggregator + 测试 |
| `percentile_tier.py` | 仅测试；tier 划分逻辑已内联到 `_score_to_tier` |
| `v0_business_rule_baseline.py` | 仅测试 |

`__init__.py` docstring 同步更新，新增 PR3a 删除记录。

---

## PR3b — YELLOW 整批删除（commit `6bdde8e`）

`-7517` 行代码，`-23` modules + `-3` CLIs + `-14` tests + `-1` compose comment。

### Modules（12 + 整个 signal_catalog/）

```
tag_pipeline.py
├── bundle_extractor.py
├── character_entity_resolver.py
├── plot_unit_segmenter.py
├── relationship_candidate_generator.py
├── rule_extractors.py
└── extractor_common.py
    ├── v0_extractor_common.py
    └── v1_extractor_common.py

tag_alignment_analyzer.py
└── match_config.py

signal_catalog/
├── __init__.py
├── llm_signals.py
└── rule_signals/
    ├── __init__.py
    ├── character.py
    ├── concept.py
    ├── dialogue.py
    ├── emotion.py
    ├── pacing.py
    └── story.py
```

### CLIs

- `app/cli/run_tag_pipeline.py`
- `app/cli/run_stability.py`
- `app/cli/run_cross_modal_alignment.py`

### Tests (14)

对应上述每个模块/CLI 的 test_* 文件。

### 副作用

- `docker-compose.dev.yml` 注释里去掉了 `cli.run_stability` 提及（`/eval` readonly 挂载
  保留，因为 `cli.ingest_dataset` 仍依赖同样的路径解析逻辑）
- `script_tools/__init__.py` docstring 同步更新到 PR3b 删除清单

---

## W5.4 端到端验证

PR1 + PR2 + PR3a + PR3b 全部叠加后，跑一次真实 reanalyze：

```
=== PR3 e2e: script_id=48f19bf0-4b92-456f-9e53-888832853b41 ===
=== DONE in 259.3s ===
meta.overall_status = degraded
chains: ['beat_chain', 'bios', 'character_graph_chain', 'compliance_chain',
         'coverage_chain', 'motivation_chain', 'reward_extractor']
  [OK ] reward_extractor          status=ok       source=llm
  [DEG] beat_chain                status=degraded source=hybrid
         : act1_filled_by_rule;act2_filled_by_rule
  [OK ] character_graph_chain     status=ok       source=llm
  [OK ] motivation_chain          status=ok       source=llm
  [OK ] bios                      status=ok       source=llm
  [OK ] compliance_chain          status=ok       source=llm
  [OK ] coverage_chain            status=ok       source=llm
scoring_runs (latest): status=done error=None
scripts: status=ready last_analysis_status=done
reports.report_json.meta.overall_status=degraded
ALL_PR3_E2E_PASS
```

**对比 PR1 端到端基准**（同一 script，257.2s）：
- 耗时基本一致（≈ 259s vs 257s），PR2 的同 model 退避未引入额外延迟
- beat_chain 这次是 `degraded source=hybrid`（PR1 那次是 `degraded source=rule_fallback`）—
  说明 PR2 W2.2 退避在 LLM 抖动时挽救了部分 act 输出，**这正是 W2.2 的设计目标**
- 其他 6 个 chain 全部 `ok source=llm`，PR3 的删除没有破坏任何 active chain

---

## 跨 PR 汇总（v1-mvp release readiness）

| PR | commit | 净 delta | 主要价值 |
|----|--------|---------|---------|
| PR1 | `6b954a0` | +2.4k lines | provenance / 状态机 / advisory lock |
| PR2 | `6cbd7cb` | +458/-41 lines (`llm_caller.py`) | 退避 / 观察性 / opt-in cache / schema 能力 |
| PR3a | `c45fc72` | -1.3k lines | 删 7 死模块 + 5 死测试 |
| PR3b | `6bdde8e` | -7.5k lines | 删 tag_pipeline 整棵子图 + 3 CLI + 14 测试 |

**累计**：删除约 **8.8k 行死代码**，新增约 **2.9k 行可观测/可靠性代码**，净瘦身 **5.9k 行**。

---

## 不在本 PR 范围（已记 plan，等独立 PR 解决）

| 项 | 拦截原因 |
|----|---------|
| 删 `models/improvement_action.py` + `scoring_improvement_actions` 表 | `rewrite_chain` 仍有 raw SQL 读该表 — 需先把 rewrite_chain 改成读 `report.evaluation.rewrite_seeds`，再删 model+migration |
| 删 `score_registry/**` | `cli/check_rubric_compat.py` 仍引用 — 评估这个 CLI 是否还要保留 |
| 删 `pacing_aggregator.py` | **永远别删**（active path 在用） |
| `LlmCache.get` 暴露 `last_hit_at` | PR2 W2.6 TTL 真生效需要这个 |
| 给 `risk_screener.quote_confirm` 加 `validate_with=` | PR2 W2.1 schema 能力的第一个使用者 |
