"""统一 ChainResult 契约（W1.2，2026-05-31）。

设计目标
========

ScriptLens 报告生成链路有 ~8 个 chain（reward / beat / graph / motivation / bios /
compliance / coverage / 6 维评分）。在 v3.7.4 之前，每个 chain 的错误处理 / 降级
方式各不相同：

- 有的 raise `ScoreLLMError` 让上层 fail-fast（如 compliance）
- 有的 try/except silent return baseline（如 character_graph_chain.enrichment 失败）
- 有的部分字段缺失就拼一条 placeholder（如 beat fallback `f"X：人物 关键场"`）
- 有的写入 `source="hybrid"`，有的不写

后果：用户 / BI / 前端无法回答"这份报告里哪些内容是 LLM 真信号，哪些是规则补位，哪些
干脆失败了？"——这是工业级 LLM Application 不可接受的 silent degradation。

`ChainResult[T]` 把所有 chain 的产出统一包成"数据 + provenance"二元组，承诺：

  1. `status` 三态（ok/degraded/failed）由调用方明确写入，禁止 silent。
  2. `source` 明确数据来源（llm/hybrid/rule_fallback），与 `status` 联动。
  3. `fallback_reasons` 保留可机读 reason 列表，前端 / 日志 / metrics 都能消费。
  4. `partial_failure_fields` 记录哪些字段是规则补位 / 缺失，前端可针对性 UX。

用法
====

每个 chain 不应再直接 `return BeatSheet(...)`，而应：

```python
async def extract_beat_sheet(...) -> ChainResult[BeatSheet]:
    try:
        sheet = await _enrich_via_llm(...)
        if sheet.fallback_reasons:
            return ChainResult.degraded(sheet, source="hybrid",
                                        reasons=sheet.fallback_reasons)
        return ChainResult.ok(sheet, source="llm")
    except ScoreLLMError as e:
        sheet = _rule_fallback(...)
        return ChainResult.failed(sheet, source="rule_fallback",
                                  reasons=[f"llm_error:{type(e).__name__}"])
```

`script_report_service` 聚合所有 ChainResult.status 到 `report.meta.chain_status`，
前端据此渲染降级提示条。

参考
====

- LangGraph `Send` API / conditional edges：失败显式状态边
- DSPy metric-driven：自动追踪 fallback 路径
- Langfuse 的 trace span "status" 字段
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, List, Literal, Optional, TypeVar

ChainStatus = Literal["ok", "degraded", "failed"]
ChainSource = Literal["llm", "hybrid", "rule_fallback"]

T = TypeVar("T")


@dataclass
class ChainResult(Generic[T]):
    """Chain 产出 + provenance 元数据。

    - `data` 是 chain 的业务输出（如 BeatSheet、CharacterGraph）。failed 状态下
      data 应该是规则降级产物，不允许为 None（除非 chain 明确文档说明）。
    - `status` 三态，由调用方根据自身判定。
    - `source` 描述数据来源；与 status 弱耦合但有约定：
        ok       → source 必须是 "llm"
        degraded → source 可以是 "llm" / "hybrid"（如部分字段规则补位）
        failed   → source 必须是 "rule_fallback"（LLM 整段失败）
    - `fallback_reasons` 是可机读的 reason 列表（如 `"act2_filled_by_rule"`、
      `"llm_error:APITimeoutError"`、`"comparable_titles_empty"`）。
    - `partial_failure_fields` 记录哪些字段是规则补位 / 空缺，前端可针对性 UX。
    """

    data: T
    status: ChainStatus
    source: ChainSource
    fallback_reasons: List[str] = field(default_factory=list)
    partial_failure_fields: List[str] = field(default_factory=list)
    chain_name: Optional[str] = None

    @classmethod
    def ok(cls, data: T, *, chain_name: Optional[str] = None) -> "ChainResult[T]":
        return cls(
            data=data,
            status="ok",
            source="llm",
            chain_name=chain_name,
        )

    @classmethod
    def degraded(
        cls,
        data: T,
        *,
        source: ChainSource = "hybrid",
        reasons: Optional[List[str]] = None,
        partial_failure_fields: Optional[List[str]] = None,
        chain_name: Optional[str] = None,
    ) -> "ChainResult[T]":
        return cls(
            data=data,
            status="degraded",
            source=source,
            fallback_reasons=list(reasons or []),
            partial_failure_fields=list(partial_failure_fields or []),
            chain_name=chain_name,
        )

    @classmethod
    def failed(
        cls,
        data: T,
        *,
        reasons: List[str],
        chain_name: Optional[str] = None,
    ) -> "ChainResult[T]":
        if not reasons:
            raise ValueError("ChainResult.failed requires at least one reason")
        return cls(
            data=data,
            status="failed",
            source="rule_fallback",
            fallback_reasons=list(reasons),
            chain_name=chain_name,
        )

    def to_status_dict(self) -> dict:
        """序列化为 `report.meta.chain_status[chain_name]` 用的 dict。

        前端只看 status 字段决定降级条配色，看 source 决定说明文案，看
        fallback_reasons + partial_failure_fields 决定 expand 详情。
        """
        return {
            "status": self.status,
            "source": self.source,
            "fallback_reasons": list(self.fallback_reasons),
            "partial_failure_fields": list(self.partial_failure_fields),
        }


def aggregate_overall_status(statuses: List[ChainStatus]) -> ChainStatus:
    """聚合多个 chain 的 status 到报告整体 status。

    规则：
    - 任一 failed → 整体 degraded（不是 failed，因为只要核心评分能算就还能用）
    - 否则任一 degraded → 整体 degraded
    - 否则 ok
    """
    if not statuses:
        return "ok"
    if any(s == "failed" for s in statuses):
        return "degraded"
    if any(s == "degraded" for s in statuses):
        return "degraded"
    return "ok"
