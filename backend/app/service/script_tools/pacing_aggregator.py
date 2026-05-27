"""Pacing curve v3: plot_unit 强度序列聚合。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import Engine

from service.script_tools.signal_catalog import SignalContext, build_signal_context
from service.script_tools.signal_catalog.rule_signals.pacing import narrative_intensity
from utils.database import engine as default_engine


@dataclass
class PacingPoint:
    episode_no: int
    plot_unit_count: int
    intensity_avg: float
    intensity_max: int
    hooks: int
    payoffs: int
    conflicts: int
    drivers_distribution: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_no": self.episode_no,
            "plot_unit_count": self.plot_unit_count,
            "intensity_avg": self.intensity_avg,
            "intensity_max": self.intensity_max,
            "hooks": self.hooks,
            "payoffs": self.payoffs,
            "conflicts": self.conflicts,
            "drivers_distribution": self.drivers_distribution,
        }


def _non_none(value: str) -> bool:
    text = (value or "").strip().lower()
    return bool(text and text != "none")


def aggregate_pacing_curve_v3(ctx: SignalContext) -> list[dict[str, Any]]:
    buckets: dict[int, list[dict[str, Any]]] = {}
    for unit in ctx.plot_units:
        ep = unit.get("episode_no")
        if ep is None:
            continue
        buckets.setdefault(int(ep), []).append(unit)

    if not buckets and ctx.plot_units:
        buckets[1] = list(ctx.plot_units)
    if not buckets:
        return []

    out: list[dict[str, Any]] = []
    for episode_no in sorted(buckets.keys()):
        units = buckets[episode_no]
        intensity_series: list[int] = []
        hooks = 0
        payoffs = 0
        conflicts = 0
        drivers_distribution: dict[str, int] = {}
        for unit in units:
            unit_id = str(unit.get("id") or "").strip()
            if not unit_id:
                continue
            intensity_series.append(narrative_intensity(ctx, unit_id))

            plot_hook = str(ctx.unit_value(unit_id, "plot_hook", default="none"))
            payoff_type = str(ctx.unit_value(unit_id, "payoff_type", default="none"))
            conflict_type = str(ctx.unit_value(unit_id, "conflict_type", default="none"))
            emotional_driver = str(ctx.unit_value(unit_id, "emotional_driver", default="none"))

            if _non_none(plot_hook):
                hooks += 1
            if _non_none(payoff_type):
                payoffs += 1
            if _non_none(conflict_type):
                conflicts += 1
            if _non_none(emotional_driver):
                drivers_distribution[emotional_driver] = drivers_distribution.get(emotional_driver, 0) + 1

        plot_unit_count = len(intensity_series)
        if plot_unit_count <= 0:
            point = PacingPoint(
                episode_no=episode_no,
                plot_unit_count=0,
                intensity_avg=0.0,
                intensity_max=0,
                hooks=hooks,
                payoffs=payoffs,
                conflicts=conflicts,
                drivers_distribution=drivers_distribution,
            )
        else:
            point = PacingPoint(
                episode_no=episode_no,
                plot_unit_count=plot_unit_count,
                intensity_avg=round(sum(intensity_series) / plot_unit_count, 4),
                intensity_max=max(intensity_series),
                hooks=hooks,
                payoffs=payoffs,
                conflicts=conflicts,
                drivers_distribution=drivers_distribution,
            )
        out.append(point.to_dict())
    return out


def aggregate_pacing_curve(
    *,
    script_id: str,
    reward_events: list[Any] | None = None,
    engine: Engine = default_engine,
) -> list[dict[str, Any]]:
    """Backward-compatible wrapper, now backed by plot_unit intensity series."""
    _ = reward_events  # retained signature only; v3 no longer depends on reward events.
    ctx = build_signal_context(script_id=script_id, engine=engine)
    return aggregate_pacing_curve_v3(ctx)
