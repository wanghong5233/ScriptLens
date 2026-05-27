from __future__ import annotations

from service.script_tools.signal_catalog import SignalContext, SignalValue, register_signal


def _evidence_units(ctx: SignalContext, *, dim: str, accepted: set[str], limit: int = 3) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for row in ctx.plot_unit_tags:
        if row.get("dim") != dim:
            continue
        value = str(row.get("value") or "").strip()
        if value not in accepted:
            continue
        unit_id = str(row.get("plot_unit_id") or "").strip()
        if not unit_id or unit_id in seen:
            continue
        seen.add(unit_id)
        out.append({"plot_unit_id": unit_id, "value": value})
        if len(out) >= limit:
            break
    return out


@register_signal("structural_completeness", scope="script", source="rule", primary_dim="story")
def compute_structural_completeness(ctx: SignalContext) -> SignalValue:
    stage_values = [value for value in ctx.plot_values("story_stage") if value and value != "none"]
    present = set(stage_values)
    matched = 0
    if {"setup", "trigger"} & present:
        matched += 1
    if "escalation" in present:
        matched += 1
    if "climax" in present:
        matched += 1
    if {"payoff", "teaser"} & present:
        matched += 1
    score = round((matched / 4.0) * 10.0, 2)
    evidence = _evidence_units(
        ctx,
        dim="story_stage",
        accepted={"setup", "trigger", "escalation", "climax", "payoff", "teaser"},
    )
    return SignalValue(
        key="structural_completeness",
        value={"matched_blocks": matched, "present_stages": sorted(present)},
        score=score,
        source="rule",
        confidence=0.85 if ctx.plot_unit_count >= 8 else 0.65,
        evidence_refs=evidence,
    )


@register_signal("reversal_effectiveness", scope="script", source="rule", primary_dim="story")
def compute_reversal_effectiveness(ctx: SignalContext) -> SignalValue:
    reversal_hooks = {
        "reversal",
        "identity_reveal",
        "secret_exposure",
        "betrayal",
        "conflict_escalation",
    }
    payoff_events = {
        "face_slapping",
        "counterattack",
        "reveal_power",
        "justice_served",
    }
    hook_values = [value for value in ctx.plot_values("plot_hook") if value in reversal_hooks]
    payoff_values = [value for value in ctx.plot_values("payoff_type") if value in payoff_events]
    unit_count = max(ctx.plot_unit_count, 1)
    intensity = (len(hook_values) + 0.5 * len(payoff_values)) / unit_count
    score = round(min(10.0, intensity * 8.0 + (2.0 if hook_values else 0.0)), 2)
    evidence = _evidence_units(ctx, dim="plot_hook", accepted=reversal_hooks, limit=4)
    return SignalValue(
        key="reversal_effectiveness",
        value={
            "reversal_hooks": len(hook_values),
            "payoff_boosters": len(payoff_values),
            "intensity": round(intensity, 4),
        },
        score=score,
        source="rule",
        confidence=0.8,
        evidence_refs=evidence,
    )


@register_signal("plot_unit_count_per_ep", scope="script", source="rule", primary_dim="story")
def compute_plot_unit_count_per_ep(ctx: SignalContext) -> SignalValue:
    episode_count = max(ctx.episode_count, 1)
    avg_units = ctx.plot_unit_count / episode_count
    if avg_units >= 6:
        score = 10.0
    elif avg_units >= 4:
        score = 8.0
    elif avg_units >= 2:
        score = 6.0
    elif avg_units >= 1:
        score = 4.0
    else:
        score = 2.0
    return SignalValue(
        key="plot_unit_count_per_ep",
        value={"avg_units_per_episode": round(avg_units, 3)},
        score=score,
        source="rule",
        confidence=0.9,
    )
