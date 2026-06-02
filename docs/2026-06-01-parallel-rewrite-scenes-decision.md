# ADR — 多场改写并行化（路线 2：batch orchestrator）

> 日期：2026-06-01
> 状态：已采纳，落地中
> 适用范围：ScriptLens agent_runtime · "按选中 N 场改写" 链路
> 关联代码：
> - `ScriptLens/backend/app/agent_runtime/service/tools/script_tools.py`
> - `ScriptLens/backend/app/service/script_tools/rewrite_chain.py`
> - `ScriptLens/backend/app/agent_runtime/prompts/script_studio/zh.yaml`
> - `RavenWeb/src/features/ScriptAnalysis/DocStudioWorkbench/index.tsx`

## 1. 背景

用户从分析报告 / RewritePlanCard 勾选 N 场（典型 3-8 场）→ 点"执行选中" →
agent ReAct 主循环串行调用 `rewrite_scene_tool` N 次，每场 LLM 改写 30-90s。

实测后果：

- 5 场场景下总耗时 150-450s，前端长时间无细粒度反馈
- `same_tool_convergence` 防线（窗口 4）会在第 4 场误伤截停（已用参数指纹化兜底）
- 链路不可观测、不可降级、单场失败让后面全断

定性结论：**多场改写天然 embarrassingly parallel**，每场 LLM 调用互不依赖、写库
互不冲突、prompt 上下文各自独立组装。**串行执行即 bug。**

## 2. 候选方案

| 方案 | LLM 决策 | 改写 prompt | 工程复杂度 | 依赖 LLM 能力 |
|---|---|---|---|---|
| A. LLM 一轮发 N 个 tool_calls（parallel tool calling） | N 个 tool_calls | 每场独立 | 大（agent_service ReAct 主循环改造、SSE/防线/history 全部多 action 化） | 高（Qwen 多 tool_calls 实测不稳：漏/乱序/重复） |
| **B. LLM 发 1 个 batch tool_call，工具内部 asyncio.gather** | 1 个 tool_call | 每场独立（gather 内逐个调用 `execute_plan_step`，prompt 与单场一字不差） | 小（新加 1 个工具 + prompt 一行 + 前端文案一处） | 低（只需 LLM 学会一次列出所有 scene_id） |
| C. 前端绕过 agent 直接打 REST 批量接口 | 不经 agent | 每场独立 | 中（破坏 agent 闭环：执行历史/审计/cancel/diff 都要另搭） | 无 |

**澄清常见误解**：路线 B 不"把 N 场塞进同一个 prompt"。LLM 决策 prompt 里
只看到 `scene_ids: [...]`（~50 字节），改写 prompt 在 `execute_plan_step` 内部
为每场独立组装（整剧概要 + 该场前后 2 场摘要 + 该场原文），**与单场调用完全等
价**，不存在"上下文撑大"的问题。

## 3. 决策

采纳 **路线 B**。

理由：

1. **工程改动最小** —— ReAct 主循环、防线、SSE 协议、history 序列化全部不动
2. **不依赖 Qwen parallel tool calling** —— LLM 只需会一次性列出 scene_ids
3. **失败聚合清晰** —— 工具内 `return_exceptions=True`，成功场照常 persist，
   失败场进 `errors[]`，agent 可以紧接着对失败场降级调单场 `rewrite_scene_tool`
4. **可降级** —— 单场场景仍可用 `rewrite_scene_tool`（不删除），路线可回滚

## 4. 实施约束

### 工具签名

```python
class ParallelRewriteScenesTool(BaseTool):
    name = "parallel_rewrite_scenes_tool"
    parameters_schema = {
        "scenes": [
            {
                "scene_id": "<uuid>",
                "target_dimensions": ["hook", ...],   # 五维子集
                "expected_changes": "...",            # 可选
            },
            ...  # 1..N
        ],
        "script_id": "<uuid>",  # 可选；缺省走 agent_state.script_id
    }
```

### 内部执行

- `asyncio.Semaphore(_MAX_PARALLEL_REWRITES)` 限速，默认 `5`
- `asyncio.gather(*tasks, return_exceptions=True)`
- gather 完毕后**串行** apply：`_persist_scene_text` + `record_rewrite_op` +
  `_mutate_agent_state_for_scene`（state 写入并发会撕，必须串行收尾）
- 任何 1 场失败不影响其它场；失败聚合到 `data.errors[]`，仍返回 `success=True`
  让 agent 看到部分结果（success=False 仅在 0 场成功时）

### convergence_key

```python
def convergence_key(self, parameters):
    return ",".join(sorted(
        str(s.get("scene_id") or "").strip()
        for s in (parameters or {}).get("scenes") or []
    ))
```

不同 scene_ids 组合 → 不同指纹 → 不会触发 `same_tool_convergence`。

### prompt 引导（`zh.yaml`）

> `mode=execute` 时，如果待改场次 ≥ 2，**必须**用 `parallel_rewrite_scenes_tool`
> 一次性派发全部场次（参数是 scene_ids 数组）。**严禁**逐场调
> `rewrite_scene_tool`（那会让用户等 N× 的时间）。单场改写、单场失败重试，
> 才用 `rewrite_scene_tool`。

### 前端 SSE 适配

- `LIVE_TOOL_LABELS`：`parallel_rewrite_scenes_tool: '并行批量改写'`
- `formatLiveToolStartLabel`：从 `parameters.scenes.length` 推断"批量改写 N 场，
  并发执行中"
- `tool_call_end` 时读 `summary` 显示 "N/M 成功" / "失败：errors[0].scene_id ..."

## 5. 未来优化方向（先记下，不在本次范围）

- **路线 A 评估**：等 Qwen-max-latest 或后续 LLM 的 parallel tool calling 稳
  定性达标后，再评估是否回到"LLM 一轮 N 个 tool_call"路线（更符合 OpenAI 协议
  原意）。届时本 ADR 与工具可一起退役。
- **流式子事件**：当前 `tool_call_start` → `tool_call_end` 之间用户看不到"已完成
  X/N 场"。可以在 batch 工具内每场完成时通过 agent_state 注入子进度事件，前端
  累计渲染。本期先打 timeline 内 N 行 timestamped 日志兜底。
- **退避与配额**：`LlmCaller` 的指数退避是 per-call；高并发场景下如果触发 429
  瞬时打满，可以在 batch 工具引入 token bucket（如 5 并发但每秒最多 3 个新启动）。
- **partial-success 重试**：当前 batch 失败场不会自动重试；agent 看到 errors[]
  后是否自动调单场 `rewrite_scene_tool` 重试一次，由后续 prompt 引导决定。

## 6. 验证清单

- [ ] 5 场场景总耗时 ≤ 1.5× 单场耗时（理想 1×，限速场景下放宽）
- [ ] 任意单场失败不影响其它场 persist
- [ ] `same_tool_convergence` 不再误伤
- [ ] 前端 timeline 能看到 "批量改写 5 场" 单条事件 + tool_call_end 的 "5/5 成功"
- [ ] 重启 docker → StatReload → agent 在 prompt 引导下能正确选用新工具
