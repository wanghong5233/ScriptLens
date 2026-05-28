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
