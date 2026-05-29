# ============================================================
# DEPRECATED — release/v1-mvp (2026-05-29)
# ============================================================
#
# 本文件属于已废弃的「整剧抽情节打标签 → rubric/signal/aggregator
# 评分」流水线（Batch3 体系）。release/v1-mvp 已切回 self-contained
# 6 维规则评分，主流程入口：
#   - service/script_tools/dimension_scorer.py
#   - service/script_report_service.py（generate_report）
# 当前已不再调用本模块任何函数。
#
# 保留原因：避免 git history 大面积污染、便于必要时回收实现细节。
# 清理时机：下次 cleanup PR 统一删除（含本文件、其测试、CLI 入口
# 与 score_registry/rubric_sets/v3.yaml 等配套资产）。
#
# 不要在本文件内再做任何功能性修改。如需新评分能力，请扩展
# dimension_scorer.py。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SharedGate(str, Enum):
    STABLE_SHARED = "stable_shared"
    AGGREGATE_ONLY = "aggregate_only"
    EXPERIMENTAL = "experimental"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class SharedTagEntry:
    dim: str
    scope: str
    gate: SharedGate
    script_verdict: str
    video_verdict: str
    reason: str
    tag_set_ver: str


# Backfilled from docs/2026-05-28-剧本标签稳定性.md
# script-side result: dialogue_density wilson_lower=0.999 (run_id=stage1_full_fast2)
SHARED_TAG_GATES: tuple[SharedTagEntry, ...] = (
    SharedTagEntry(
        dim="dialogue_density",
        scope="plot_unit",
        gate=SharedGate.STABLE_SHARED,
        script_verdict="stable_shared_candidate",
        video_verdict="pending",
        reason="script wilson_lower=0.999 from stage1_full_fast2",
        tag_set_ver="v2.0.0",
    ),
)


def list_entries() -> tuple[SharedTagEntry, ...]:
    return SHARED_TAG_GATES


def list_dims_by_gate(gate: SharedGate | str) -> tuple[str, ...]:
    gate_value = SharedGate(gate)
    return tuple(entry.dim for entry in SHARED_TAG_GATES if entry.gate == gate_value)


def get_entry(dim: str) -> SharedTagEntry | None:
    for entry in SHARED_TAG_GATES:
        if entry.dim == dim:
            return entry
    return None
