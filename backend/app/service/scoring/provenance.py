"""scoring v4 chain provenance：与 script_tools.chain_result.ChainResult 对齐。

设计要点：
- 后端可观测详细记录（每个 signal 的 status / source / fallback_reason）
- 前端仅在 `?debug=1` 展示（业务策略：不向最终用户暴露技术降级提示）
- 写入日志的字段对齐 docs/2026-05-31-pr1-release-readiness-wave1.md 中的 ChainResult 模式
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from service.scoring.framework import DimensionScore, SignalStatus

logger = logging.getLogger(__name__)


@dataclass
class ScoreChainRecord:
    """单 dimension 的 chain 状态记录。"""

    dimension: str
    overall_status: str          # ok | degraded | failed
    fallback_reasons: list[str] = field(default_factory=list)
    partial_failure_fields: list[str] = field(default_factory=list)
    signal_count_total: int = 0
    signal_count_computed: int = 0
    signal_count_degraded: int = 0
    signal_count_failed: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "overall_status": self.overall_status,
            "fallback_reasons": list(self.fallback_reasons),
            "partial_failure_fields": list(self.partial_failure_fields),
            "signal_count_total": self.signal_count_total,
            "signal_count_computed": self.signal_count_computed,
            "signal_count_degraded": self.signal_count_degraded,
            "signal_count_failed": self.signal_count_failed,
            "extra": dict(self.extra),
        }


def make_record(dimension: DimensionScore) -> ScoreChainRecord:
    """从 DimensionScore 派生 chain status record。

    fallback_reasons 取自所有 DEGRADED / FAILED signal 的 fallback_reason。
    """
    total = len(dimension.signals)
    computed = sum(1 for s in dimension.signals if s.status == SignalStatus.COMPUTED)
    degraded = sum(1 for s in dimension.signals if s.status == SignalStatus.DEGRADED)
    failed = sum(1 for s in dimension.signals if s.status == SignalStatus.FAILED)

    if failed > 0 and computed + degraded == 0:
        status = "failed"
    elif failed > 0 or degraded > 0:
        status = "degraded"
    else:
        status = "ok"

    fallback_reasons: list[str] = []
    partial_failure_fields: list[str] = []
    for sig in dimension.signals:
        if sig.status in (SignalStatus.DEGRADED, SignalStatus.FAILED) and sig.fallback_reason:
            fallback_reasons.append(f"{sig.key}: {sig.fallback_reason}")
        if sig.status == SignalStatus.FAILED:
            partial_failure_fields.append(sig.key)

    return ScoreChainRecord(
        dimension=dimension.key,
        overall_status=status,
        fallback_reasons=fallback_reasons,
        partial_failure_fields=partial_failure_fields,
        signal_count_total=total,
        signal_count_computed=computed,
        signal_count_degraded=degraded,
        signal_count_failed=failed,
    )


def log_chain_record(record: ScoreChainRecord, script_id: str) -> None:
    """后端结构化日志。便于 BI / 报警。"""
    logger.info(
        "scoring.chain script=%s dim=%s status=%s computed=%d degraded=%d failed=%d "
        "fallback=%s",
        script_id,
        record.dimension,
        record.overall_status,
        record.signal_count_computed,
        record.signal_count_degraded,
        record.signal_count_failed,
        ";".join(record.fallback_reasons[:3]) if record.fallback_reasons else "none",
    )


__all__ = ["ScoreChainRecord", "log_chain_record", "make_record"]
