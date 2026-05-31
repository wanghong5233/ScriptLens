"""scoring v4 投放回流校准 hook（架构预留，本期不强制使用）。

将来扩展：
- record_ad_performance(script_id, payload)：BFF 回填投放数据
- compute_threshold_calibration(rubric_version)：离线 job，跑回归推荐 verdict_cuts 更新

数据落 scoring_runs.ad_perf_payload 字段（Wave C 加列）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AdPerformancePayload:
    """投放回流数据（与 scoring_runs.ad_perf_payload jsonb 字段对齐）。"""

    release_at: str
    completion_rate: float
    paid_conversion: float
    roi: float
    hit_label: str                   # "hit" | "mid" | "flop"
    labeled_at: Optional[str] = None


def record_ad_performance(script_id: str, payload: AdPerformancePayload) -> None:
    """占位实现：仅记日志。

    待 BFF 路由就绪后实现真实落库（scoring_runs.ad_perf_payload = payload.__dict__）。
    """
    logger.info(
        "scoring.calibration.record script=%s hit=%s roi=%.2f",
        script_id,
        payload.hit_label,
        payload.roi,
    )


def compute_threshold_calibration(rubric_version: str) -> dict[str, float]:
    """占位实现：暂返回当前阈值。

    待数据累积到 N 个样本后实现回归求最优 verdict_cuts。
    """
    logger.info(
        "scoring.calibration.compute (placeholder) rubric=%s", rubric_version
    )
    return {}


__all__ = [
    "AdPerformancePayload",
    "compute_threshold_calibration",
    "record_ad_performance",
]
