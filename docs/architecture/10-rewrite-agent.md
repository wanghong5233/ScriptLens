# ScriptLens Rewrite Agent

## 1. 当前形态

改写链路固定为 **plan → execute**，并通过 ScriptVFS 把数据库场景映射为虚拟文件路径，复用既有 diff 审阅机制。

核心约束：

- 前端只派发用户意图 + `<TASK_META>`，不在前端拼长 prompt。
- 后端改写能力拆为三件套：`read_scene_tool` / `propose_full_script_plan_tool` / `rewrite_scene_tool`。
- 旧工具 `propose_dimension_rewrite_tool` 仅保留兼容转发，不再作为主能力面。

---

## 2. 虚拟文件契约（ScriptVFS）

### 2.1 路径规范

- 统一路径：`scenes/E{episode}-S{scene:03}.txt`
- 支持多位集号（如 `E100-S001`），场号固定三位。
- 输入可接受 `scene_id` 或 `file_path`，内部必须归一化到 `file_path`。

### 2.2 状态契约

改写后 AgentState 必须满足：

- `state.modified_files`：存放虚拟路径（`scenes/E..-S...txt`）。
- `state.original_file_contents[path]`：首次改写前原文快照。
- `state.original_file_contents[scene_id]`：兼容历史键，不作为主键使用。

### 2.3 可逆写入

- 执行改写：`UPDATE scriptlens.scenes.text`。
- 撤销/拒绝：仍走场景内容更新接口回写原文。
- diff 生成统一读取：`original = state.original_file_contents[path]`，`modified = ScriptVFS.read(path)`。

---

## 3. 改写三件套

| 工具 | 输入 | 输出 | 侧效应 |
|---|---|---|---|
| `read_scene_tool` | `scene_id` 或 `file_path` | 场景元数据 + 原文 | 无 |
| `propose_full_script_plan_tool` | `dimensions[]` | `rewrite_plan`（steps） | 无 |
| `rewrite_scene_tool` | `scene_id/file_path` + `target_dimensions[]` + `expected_changes` | 单场改写结果摘要 | 写库 + mutate AgentState |

兼容工具：

- `propose_dimension_rewrite_tool`：只做协议兼容，内部转发到三件套。

---

## 4. TASK_META 协议

```json
{
  "kind": "fulltext_rewrite",
  "mode": "plan",
  "dimensions": ["emotion", "story"]
}
```

映射规则：

- `kind=fulltext_rewrite, mode=plan` → `propose_full_script_plan_tool`
- `kind=fulltext_rewrite, mode=execute` → 对 `plan_steps` 逐条调用 `rewrite_scene_tool`
- `kind=rescore` → 按维度逐条调用 `score_dimension_tool`
- `kind=dim_inquiry` → `score_dimension_tool`
- `kind=evidence_lookup` → `read_scene_tool`（可选）+ `score_dimension_tool`

同条消息内若存在自然语言附加要求，以自然语言约束优先，`TASK_META` 仅提供结构化字段。

---

## 5. 前端派发与渲染契约

### 5.1 派发

- `dispatchAgentTask` 只负责生成用户消息 + `<TASK_META>`。
- 选区文本在请求侧使用 `<SELECTION>...</SELECTION>` 包装后发送给 Agent。
- 自动派发（`autoSubmit`）不污染输入框内容。

### 5.2 渲染

- 根据返回 **data shape** 渲染，不绑死工具名：
  - 存在 `rewrite_plan.steps[]` → 渲染计划卡。
  - 存在 `file_diffs[]` → 进入 diff 审阅流程。
  - 其余按普通 Agent 回复渲染。

---

## 6. Prompt 解耦原则

`zh.yaml` 仅保留两类信息：

1. 工具能力契约（输入/输出/适用场景）
2. `TASK_META` 路由规则

不再承载：

- 组件名、面板方位、按钮文案等 UI 细节
- “禁止说某句话”类型补丁
- 与实现细节强耦合的临时行为说明

目标：Prompt 只描述“能力与协议”，UI 和编排逻辑留在前端/服务层实现。
