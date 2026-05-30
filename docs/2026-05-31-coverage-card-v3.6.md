# Coverage Card v3.6 — 加 comparable_titles

## 后端契约变更

`CoverageCard`（`schemas/script.py` + `service/script_tools/coverage_chain.py`）新增字段：

```python
comparable_titles: List[str] = Field(
    default_factory=list,
    description="同类爆款 2-3 部，每条 ≤ 16 字"
)
```

## LLM Prompt 规则

在 `_PROMPT` JSON schema 中追加 `comparable_titles` 字段，并在【重要规则】第 7 条要求：

- 2-3 部题材接近、规模相当的**已成爆款短剧 / 漫剧**（抖音红果 / 快手星芒 / WeTV / ReelShort 头部投放剧）
- 每条 ≤16 字，可以是「剧名」或「剧名 · 短描述」（如「《无双》逆袭复仇模板」）
- 优先选**同赛道 + 同题材**真实存在剧目；不要编造
- 如果无可类比，最多 1 条或空数组，不要凑数

## 解析

`extract_coverage_card` 用 `_string_list(..., limit=3, item_max=16)` 解析；返回 `CoverageCard.comparable_titles`。

## 透传

`script_view_service.py` 已用 `coverage_card=report.coverage_card` 透传 Pydantic 模型，无需额外映射。

## 业内对照

抖音红果选品 / 快手星芒判断必看「同类爆款」做对照锚点：
> 「这部剧像哪部已成爆款？借鉴度多高？」

是短剧买手 30 秒决策中权重最高的信号之一。
