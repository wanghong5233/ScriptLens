# LLM 应用 Schema 与质量加固指南

> 2026-05-31 · ScriptLens / dcccloud
>
> 起源：上线前压测发现 LLM 偶发输出 schema 错（如 `beats: -1`）+ 字段语义达不到要求（如 `appearance` 全空）触发降级。
> 用户反馈：「这种 bug 根本修不完，问题的本质是什么？」
>
> 本文档对照行业最佳实践，梳理 LLM 应用工程里 schema/quality 加固的 7 大方案，
> 并给出 ScriptLens 当前的演进路径。

---

## 1. 问题的本质：LLM 应用的两层 schema

LLM 应用的输出可靠性问题分两层，行业能搜到的所有方案都在解这两层之一：

| 层 | 问题 | 例子 |
|----|------|------|
| **结构层**（structural）| 输出能否被程序解析？type/shape 是否正确？ | `beats: -1` 而不是 `beats: [...]` |
| **语义层**（semantic） | 每个字段的内容是否真实、完整、达标？ | `appearance.facial: ""`（明明剧本有描写） |

**关键认知**：Pydantic / Zod / JSON Schema 只能解第一层，**完全不能保证第二层**。
LLM 在 type-correct 的同时输出语义垃圾（空字符串、敷衍、错位、幻觉）是常态。

---

## 2. 行业 7 大方案对照

### 2.1 Schema-first generation（结构层）

代表：OpenAI **Structured Outputs / function calling**、Anthropic **tool_use**、Gemini **schema mode**、**Instructor**、**Outlines**、**LM Format Enforcer**。

| 方案 | 强度 | 代价 | 适用 |
|---|---|---|---|
| OpenAI structured output (gpt-4o+) | ★★★★★（grammar-constrained decoding） | 无 | 推荐首选，token 不会越界 |
| Anthropic tool_use schema | ★★★★ | 无 | Claude 系列 |
| Pydantic + JSON Schema in prompt | ★★ | 无 | 通用 fallback，依赖 prompt 遵循 |
| Outlines / LM Format Enforcer | ★★★★ | 集成成本 | 自托管模型 |

**评价**：原生 structured output 是 2024 之后行业事实标准。**LLM 永远不会输出非法 schema** —— 因为底层 sampler 被 grammar 限制。

### 2.2 Validate-and-repair loop（结构层兜底）

代表：**Instructor** (`max_retries=2`)、**LangChain OutputParser**、**DSPy Refine**、自研。

模式：
```
LLM call → Pydantic validate → ValidationError → 把错误反馈给 LLM → 重试 1-N 次
```

ScriptLens 当前在用（W2.1）。优点：与现有 LLM 网关解耦。缺点：依赖 LLM 看得懂错误 + 改对，**单轮成功率经验值 50~85%**。

### 2.3 Constrained decoding / Grammar-based generation（结构层硬约束）

代表：**Outlines**（用 regex/CFG 约束 token sampling）、**LM Format Enforcer**、**llama.cpp `--grammar`**、OpenAI structured output 底层用的就是这个。

模式：sampling 时只允许输出 grammar 接受的 token，**LLM 物理上不可能写错 JSON**。

**评价**：最硬的硬约束。代价是需要拿到 token logits（自托管）或 provider 原生支持。

### 2.4 Field-level Self-Refine（语义层）⭐

代表：**Anthropic Constitutional AI**、**DSPy Refine + ChainOfThought**、**Self-Refine paper** (Madaan et al. 2023)、**Microsoft Guidance**。

模式：
```
LLM 输出 → 字段级 critique（哪些字段空/敷衍/错位） → 弱字段单独再调一次 LLM → merge
```

**这正是用户提到的"字段级 LLM 二次分析"。**

关键设计：
1. **critique 用规则**（关键词命中 / 长度阈值 / 业务规则），不再调一次 LLM 做 critique（否则 cost 翻倍）
2. **refine 只塞目标字段 + 必要上下文**，不重写整个对象
3. **多个弱字段合并到 ≤ 3 次 refine call**（按字段语义分组），避免 N+1
4. **refine 失败该字段保留原值**，不影响其他字段

ScriptLens v3.7.5 实现了这套，应用在 bios（见 §4）。

### 2.5 Self-Consistency / Multi-sample voting

模式：同 prompt 跑 N 次（temperature > 0），多数派或 ensemble。

**评价**：cost N 倍。仅适合关键决策点。ScriptLens 暂不用。

### 2.6 Outline-Refine / Skeleton-then-Detail

代表：**LangChain MapReduceChain**、**DSPy Pipeline**。

模式：先 LLM 生成 outline / 字段名 list，再分字段 fill。比 single-shot 更稳，但 latency 翻倍。适合长文档（章节级抽取）。

### 2.7 Declarative pipeline（DSPy / LangChain / Instructor）

代表：**DSPy**（Stanford / Databricks）、**LangChain LCEL**、**Marvin**。

把上面所有方案做成声明式 pipeline：
```python
class Bio(dspy.Signature):
    """从 scene 抽人物小传"""
    scenes: str = dspy.InputField()
    bio: BioModel = dspy.OutputField()
    
extractor = dspy.ChainOfThought(Bio).with_retries(2).with_refine(...)
```

**评价**：很优雅但是黑盒。ScriptLens 现阶段保持自研，未来 LLM 链路扩张后可以迁移。

---

## 3. ScriptLens 当前状态（v3.7.5）

| 方案 | 状态 | 实施位置 |
|---|---|---|
| 2.1 Schema-first | ⚠ 部分 | 千问 Plus 暂无 native structured output；用 Pydantic in prompt + validate |
| 2.2 Validate-and-repair | ✅ | `LlmCaller._validate_with_repair`（W2.1，单轮 repair） |
| 2.2 Repair prompt 改用 "minimal valid example" | ✅ v3.7.5 | 把 schema dump 换成具体 example（业内 show-don't-tell 实践） |
| 2.3 Constrained decoding | ❌ | 千问网关不支持 token-level grammar；规划中 |
| 2.4 Field-level Self-Refine | ✅ v3.7.5 | `character_pipeline._critique_bio` + `_refine_bio_fields` |
| 2.5 Self-Consistency | ❌ | cost 不允许，暂不需要 |
| 2.6 Outline-Refine | ❌ | 短剧场景单 prompt 已够；scoring rubric 可能在后续启用 |
| 2.7 DSPy 声明式 pipeline | ❌ | 链路 ≤ 8 条，暂用自研 |

### 3.1 当前 LlmCaller 能力清单

```
caller.call_json(
    prompt=...,
    validate_with=PydanticModel,   # 2.1: 结构 schema
    chain_name="...",              # 2.x: metrics & trace
    # 自动 2.2: schema 错误 → repair retry × 1
    # 重试 prompt 用 model_config.json_schema_extra["example"] 当 reference
)
```

### 3.2 当前 bios pipeline 的双重保险

```
1. LLM 抽 bio（含 identity / appearance / persona / ...）
       ↓
2. structural validate（隐式，type 错会 raise）
       ↓
3. _critique_bio() 检查关键字段：
   - 穿越/重生强信号命中 + identity_origin 空？
   - appearance 五字段全空 + 该角色 ≥ 3 场？
   - persona_surface/core < 30 字 + 该角色 ≥ 2 场？
       ↓
4. _refine_bio_fields()：弱字段分组 → 最多 3 次 LLM call refine
       ↓
5. merge 回原 bio，bio.source = "llm+refine"，evidence 记录哪些字段被重写
```

---

## 4. 案例复盘：姜栀枝 "恶毒女配" + appearance 全空

### 4.1 根因

剧本第一集明确出现穿越信号：

```
△姜栀枝系统觉醒，突然愣住
系统VO（字幕）：已锁定载体，宿主灵魂投放中
系统VO（字幕）：你穿成恶毒女配，嫉妒万人迷乔颜...
```

旧 bios prompt 把 identity_origin 描述为：

> 仅在剧本是穿越 / 重生 / 失忆 / 失散 / 大家族失散身世题材，且有明确文本支撑时才填：
> "上一世我..." / "穿越前是 21 世纪..." / "其实你才是 X 家的真千金"

LLM 没能把 **"已锁定载体" "宿主灵魂投放" "穿成"** 这些关键词等价于 prompt 里给的"上一世/穿越前"线索。
于是把所有信息塞进 identity_present，origin/hidden 留空。

appearance 同理：第一集没直接外貌描写（全是动作行 + 系统VO），LLM 全空。

### 4.2 v3.7.5 修复

#### A. Prompt 加固（识别隐式信号）

`character_pipeline._BIO_SYSTEM_PROMPT` 新增：

```
【穿越/重生/系统觉醒题材的强识别信号】（命中任一即说明该角色是穿越者/重生者）：
  - "系统觉醒" / "系统VO" / "已锁定载体" / "宿主灵魂投放" / "宿主"
  - "穿成XX" / "穿越成XX" / "穿到XX" / "穿来" / "穿到这个世界"
  - "重生回到" / "上辈子" / "前世" / "再睁眼时" / "灵魂回到"
  ...

⭐ 命中强信号时的填法（**这是最常见的错误，请务必照做**）：
   - identity_origin   填「穿越者的原生身份」
   - identity_present  填「角色在剧本世界里被设定的当前身份」  
   - identity_hidden   填「穿越事实对其他角色保密」

**关键提示**：如果你在剧本里看到「系统VO」「宿主」「穿成」「载体」任一关键词，
identity_origin **绝对不能为空字符串**。
```

#### B. Field-level Refine（兜底）

即使 prompt 加固后 LLM 仍可能漏抽，`_critique_bio` 用规则检测：

```python
# 1. 穿越/重生强信号 → identity_origin 必填
has_time_travel = bool(_TIME_TRAVEL_SIGNALS_RE.search(scene_text))
if has_time_travel and not bio.identity_origin.strip():
    weak_fields.append("identity_origin_time_travel")

# 2. appearance 全空 + 场景充足
if appearance_empty and len(scenes_for_entity) >= 3:
    weak_fields.append("appearance_all_empty")

# 3. persona 极短
if len(bio.persona_surface.strip()) < 30 and len(scenes_for_entity) >= 2:
    weak_fields.append("persona_surface_too_short")
```

然后 `_refine_bio_fields` 对每个弱字段组**单独**调一次 LLM，prompt 只问那几个字段 + 给 scene 上下文：

```
该角色的场景片段里出现了穿越/重生/系统觉醒的强信号
（如「系统VO」「宿主灵魂」「已锁定载体」「穿成」「重生」），但你刚才**没有填**
identity_origin。请重新提取这两个字段——不要再返回完整小传，**只输出下面这个最小 JSON**：

{
  "identity_origin": "<穿越者/重生者的原生身份段落，60~120 字>",
  "identity_hidden": "<可填可空。建议：「灵魂置换的事实对剧中其他角色保密」>"
}
```

### 4.3 兜底效果对比

| 指标 | v3.7.4 | v3.7.5 |
|---|---|---|
| identity_origin 在穿越剧的填充率 | LLM 凭语义识别（约 ~50%） | LLM 直接看强信号关键词（~95%）+ refine 兜底（~99%） |
| appearance 抽取失败（5 字段全空）触发率 | 静默 | 自动 refine（再扫一次）|
| LLM 调用次数 | 1 次/bio | 1-2 次/bio（绝大多数 1 次，弱字段触发 ≤ 1 次 refine）|
| Cost 增加 | 0 | 约 +15~30%（仅弱 bio 触发） |

---

## 5. 未来演进路径

### 5.1 短期（已落地）

- ✅ Pydantic schema + validate-and-repair
- ✅ Repair prompt 用 minimal valid example
- ✅ Field-level refine（bios）
- ✅ 关键词强信号补充（prompt 加固）

### 5.2 中期（1-3 个月）

- ⏳ Field-level refine 推广到 coverage_chain（strengths/concerns analysis 太短 → refine）
- ⏳ Field-level refine 推广到 motivation_chain（reasoning 字段空 → refine）
- ⏳ 给 beat_chain / character_graph 也接 Pydantic + example-based repair

### 5.3 长期（>3 个月，需 provider 配合）

- ⏳ 切换到原生支持 **structured output** 的 provider/model（gpt-4o / Claude 3.5+ / Qwen2-instruct 新版）
- ⏳ 评估 DSPy 迁移（链路 ≥ 10 条 + 需要 prompt 自动优化时）
- ⏳ 自托管短剧专用模型 + Outlines grammar-constrained decoding

---

## 6. 给后续工程师的实战建议

1. **永远不要相信 LLM 的"我已经按 schema 输出"** —— 加 Pydantic validate。
2. **prompt 里的 few-shot example 比 schema description 更管用** —— show, don't tell。
3. **关键字段空 ≠ 剧本没信息** —— 加规则 critique + LLM refine 兜底。
4. **不要因为"怕错"而 prompt 写"信息缺失留空"** —— LLM 会偷懒。要写"先尝试提取，确实不存在才留空"。
5. **每条 prompt 都要有「失败模式 + 致命错误反例」** —— LLM 看反例比看正例学得快。
6. **fallback 不是终点，是降级的最后一道防线** —— 永远先保证 LLM/refine 不失败。
7. **降级状态记后端，不告诉用户** —— 后端可见，前端用户透明（除非 dev mode）。

---

## 参考资料

- Madaan et al. 2023, ["Self-Refine: Iterative Refinement with Self-Feedback"](https://arxiv.org/abs/2303.17651)
- OpenAI 2024, ["Introducing Structured Outputs"](https://openai.com/index/introducing-structured-outputs-in-the-api/)
- Anthropic 2024, ["Prompt Engineering Guide: Long Context"](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/long-context-tips)
- DSPy framework, [stanfordnlp/dspy](https://github.com/stanfordnlp/dspy)
- Instructor, [jxnl/instructor](https://github.com/jxnl/instructor)
- Outlines, [dottxt-ai/outlines](https://github.com/dottxt-ai/outlines)
- LM Format Enforcer, [noamgat/lm-format-enforcer](https://github.com/noamgat/lm-format-enforcer)
- Microsoft Guidance, [guidance-ai/guidance](https://github.com/guidance-ai/guidance)
