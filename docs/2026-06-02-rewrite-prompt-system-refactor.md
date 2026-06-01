# ADR: rewrite_chain prompt 系统重构（dimension-aware + critic 二阶段）

- **日期**: 2026-06-02
- **状态**: Accepted
- **影响范围**: `ScriptLens/backend/app/service/script_tools/rewrite_chain.py` 及其调用方
  （RavenWeb DocStudioWorkbench / agent_runtime ScriptTools）
- **关联代码**:
  - alembic migration `12_add_scenes_brief_json`
  - prompt 资源 `service/script_tools/prompts/script_studio/**`
  - `service/script_tools/prompt_loader.py`
  - 测试 `tests/test_rewrite_chain.py`

## 背景：为什么改

v1-mvp 的 `propose_plan` / `execute_plan_step` 链路在多个用户报告里输出"胡说八道"
的改写建议。典型 case（截图来自报告分析入口 producibility 维度改写）：

> 「该场次中顾聿之和裴鹤年同时出现，增加了跨集角色复现的责任。建议只保留顾聿之
> 一人，或者将裴鹤年的部分移到另一场次。」

裴鹤年和顾聿之是**双男主**。把男一删掉等于把这部剧改没了。这条建议 **方向完全反了**。

根因不是 LLM 能力不足，是 **prompt 给 LLM 的上下文严重缺失**：

| 缺失项 | 后果 |
| --- | --- |
| 维度知识为零（LLM 只拿到 `目标维度: producibility` 一行） | LLM 脑补"复杂度=多角色→删角色" |
| 角色身份断档（`characters` 是顿号拼接的纯字符串） | LLM 不知道谁是主角 |
| 场次摘要 110 字 + 省略号末截 | LLM 看不到角色台词分布 / 冲突走向 |
| output_contract 第 5 条机械化规则（"优先挑同框人数多的场"） | 12 场 plan 套同一句模板话术 |
| 评分阶段已有的 signal/evidence/tier_anchor 信息 | 在 plan 阶段被完全丢弃 |

值得注意的是评分阶段做得很专业：`service/scoring/rubrics/cn_short_drama.yaml` 含
5 维 × 多 signal，每 signal 都标了 tier_anchor / tier_scores / 抖音 SOP 引用。
`ImprovementAction` 也已经带 `dimension_key + signal_key + evidence_ref_ids`。
**plan / execute 链路只是没用上这份评分知识。**

## 决策

按照 OpenAI Cookbook / DSPy / LangChain LLM-as-judge 的链路做法重构成 3 层结构：

```
┌──────────────────────────────────────────────────────────────────┐
│ Layer 1 — 数据准备                                                │
│  • scriptlens.character_entities → role_map 注入                   │
│  • scriptlens.scenes.brief_json（新加字段，预留 on-demand 生成）   │
│  • scene_catalog 按 主/反/配/龙 4 桶渲染                            │
├──────────────────────────────────────────────────────────────────┤
│ Layer 2 — Planner LLM                                            │
│  prompt = system + 维度专属方法论 + brief + catalog + output      │
│  • system: 第一性原理（主角不可压缩 / producibility 真实含义 / ...） │
│  • by_dimension/{hook,archetype,payoff,monetization,producibility}.zh.md │
│    — 每维 1k 字方法论 + Bad/Good rationale 示例                    │
│  • output_contract: 主角禁删 + 模板话术零容忍                       │
├──────────────────────────────────────────────────────────────────┤
│ Layer 3 — Critic LLM（同模型不同 prompt）                          │
│  • 审 rationale 是否含具体角色名 / 具体冲突                          │
│  • 审 expected_changes 是否触碰 protagonist/antagonist             │
│  • 合格→kept、能救活→改写后 kept、不能救活→dropped                  │
│  • Critic 失败 / 返回 planner 格式 → 自动 fallback 到 planner 输出  │
└──────────────────────────────────────────────────────────────────┘
```

### 关键技术选型

| 选项 | 选择 | 理由 |
| --- | --- | --- |
| prompt 格式 | `.md + str.format` | 编辑器友好，diff 友好；JSON 字面用 `{{...}}` 转义。比 jinja2 轻量、比 yaml 表达力更强。 |
| 维度模板组织 | `prompts/script_studio/{plan,execute,critic}/...` | 评分用 `service/scoring/prompts/` 也是按维度分文件，保持组织口径一致 |
| 维度 prompt 注入策略 | 只注入主维度 | improvement_brief.dimension_key 决定主维；多维并行（target_dimensions 多个）也只注入第一个，避免 prompt 撑爆 |
| 主角名单来源 | `character_entities.role` 字段 | 复用 character_pipeline 已有的判定（_role_of 按出场场次 + LLM enrichment 覆盖），不再造一个 role-classifier |
| critic 失败策略 | best-effort, fallback to planner | Critic 是质量增强，不应该让 critic 故障吃掉 planner 的合理输出 |
| scene_brief 落库 | 加字段 + 留生成器 TODO | 字段已就绪，生成器单独 commit；plan/execute 在 brief 为 NULL 时 fallback 到 digest |

### producibility 误读纠正（这是本次重构的核心痛点）

`prompts/script_studio/plan/by_dimension/producibility.zh.md` 显式纠正业内误读：

> AI 漫剧的成本敏感点是「次要角色 LoRA 摊薄不下来」、「换景频次过高」、
> 「群戏（>5 人同框）渲染贵」、「无台词工具人浪费 token」。**所以减的是
> 配角/龙套/工具人，不是主角**。

prompt 直接把那条用户截图的 Bad rationale 写进了 Bad case few-shot：

```
**Bad rationale**（典型胡说八道，会被 critic 退回）：
> 「该场次中顾聿之和裴鹤年同时出现，增加了跨集角色复现的责任。建议只保留
> 顾聿之一人，或者将裴鹤年的部分移到另一场次。」
>
> ↑ 这条建议错在哪：顾聿之、裴鹤年都是主角（双男主），他们的同框
> 是 LoRA 摊薄的优势场景，**不是**复现负担。这条建议方向反了。
```

这是 in-context learning 的硬反例 — LLM 看到这个例子就会主动避开同样的错误。

## 不动什么

- **scoring rubric**（`cn_short_drama.yaml`）保持不动 —— scoring 链路已经在生产，
  改 rubric 等于重新跑历史评分。本次只是让 plan/execute 链路开始**消费**这份 rubric。
- **agent_service 的 same_tool_convergence guardrail** 维持上一轮的修复
  （BaseTool.convergence_key 默认返回 ""）—— critic 二阶段会调用同 LLM，不会触发
  convergence 误判（critic 走的是不同 chain_name "rewrite_plan_critic"）。
- **执行结果的持久化路径** 不变 —— `script_operation_service` 仍负责落库，
  `RewriteResult` 的 schema 没动。
- **API 路由** 不变 —— `propose_plan / execute_plan_step` 函数签名向后兼容，
  RavenWeb 端不需改前端代码。

## 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| Critic 增加 1 次 LLM 调用 ⇒ plan 响应时间约翻倍 | 已用 best-effort fallback；如延迟不可接受，可在 propose_plan 加 `enable_critic=False` 参数关闭 |
| prompt md 加载失败（文件缺失 / 容器没挂载）⇒ propose_plan 启动报错 | loader 内置 LRU 缓存，单次失败重启不会一直卡死；CI 部署时 grep 5 维 md 文件存在性 |
| 5 维方法论 prompt 之间风格 / 长度差异大 ⇒ 维度间的 plan 质量参差 | producibility 是当前最痛维度，做得最详细（1.8k 字）；其它 4 维 ≈ 1k 字够用。后续根据用户反馈针对性扩 few-shot |
| scene_brief 生成器还没接入 ⇒ plan/execute 仍只能看 110 字 digest | 当前 commit 仅准备字段；下个 commit 接入 `_ensure_scene_briefs`，prompt 层无需再改 |

### 回滚

如需回滚整个重构：

1. `git revert <本次 commit>` —— 恢复 rewrite_chain.py
2. `alembic downgrade -1` —— 删 `scenes.brief_json` 字段
3. `rm -rf service/script_tools/prompts/script_studio` —— 删模板文件
4. `rm service/script_tools/prompt_loader.py` —— 删 loader

测试套件 6/6 → 8/8 全绿，回滚后会回到 6/6。

## 验证

- `tests/test_rewrite_chain.py`: 8/8 pass
  - 新增 `test_plan_prompt_includes_dimension_guidance_and_first_principles`：
    确认 producibility plan prompt 含维度方法论 + 第一性原理 + 输出契约硬约束
  - 新增 `test_critique_plan_fallback_when_critic_returns_planner_shape`：
    确认 critic 跑偏时不会误杀 planner 输出
- `tests/test_scoring_v4_payload_contract.py`: 4/4 pass（无回归）

### 待人工验证

- 重启后端 → 重新发起一次 producibility 改写 → 检查 plan 是否不再"建议删主角"
- 多轮 LLM 一致性：跑 5 次同一 improvement_brief，看 plan 是否稳定输出"减次要角色"
  类的建议（不是再次胡说"减主角"）

## 后续

- **C1c**: `_ensure_scene_briefs` LLM 生成器 + 接入 propose_plan 主路径
  （只对 priority_scene_ids 做，控制延迟）
- **可选 C6**: critic 升级为带 evidence 引用的多轮迭代（DSPy ChainOfThought 风格）
- **可选 C7**: 把 5 维 md 风格 / 长度对齐 + 抽 jinja2 partials 复用 few-shot 头
