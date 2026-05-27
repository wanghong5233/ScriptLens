# V3 Improvement Actions Review (Dev)

- rubric_version: `v3.0.0`
- actionability threshold: `>= 0.7`
- measured actionability: `0.6667`
- sample_size: `3` scripts
- verdict: `FAIL`（低于门槛）

## Per-Script Actionability

- script#1: `1.0000`
- script#2: `1.0000`
- script#3: `0.0000`（该样本未产出有效改写动作）

## Example Action (from scoring smoke)

- dimension: `character`
- signal_key: `protagonist_agency`
- issue: `主角存在决策，但关键时刻的主动性表达不足。`
- target: `提升主角“主动提出-执行-反馈”的完整链条。`
- action_steps:
  - `增强主角发起动作的台词或行为。`
  - `补一条结果反馈，展示主角决策影响。`
- estimated_lift: `{"character": 0.8, "story": 0.2}`

## Review Notes

- 当前模板化动作在有 evidence 的样本上质量较高，但样本间覆盖不均衡。
- 下一轮优先补齐低分样本对应信号模板，特别是 `poor/weak` 维度未产出 action 的场景。
- 建议在 acceptance 阶段追加“每剧至少 1 条动作”的硬性约束，防止均值被头部样本抬高。
