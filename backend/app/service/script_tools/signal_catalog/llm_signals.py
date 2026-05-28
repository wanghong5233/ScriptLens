from __future__ import annotations

from typing import Any

from service.score_registry import RubricConfig, load_prompt_by_bundle
from service.script_tools.extractor_common import render_prompt
from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget
from service.script_tools.signal_catalog import SignalContext, SignalValue


def _safe_float(raw: Any, default: float | None = None) -> float | None:
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _build_plot_unit_block(ctx: SignalContext, *, max_units: int = 36) -> str:
    lines: list[str] = []
    for unit in ctx.plot_units[:max_units]:
        unit_id = str(unit.get("id") or "")
        hook = ctx.unit_value(unit_id, "plot_hook", default="none")
        conflict = ctx.unit_value(unit_id, "conflict_type", default="none")
        payoff = ctx.unit_value(unit_id, "payoff_type", default="none")
        summary = str(unit.get("summary") or "").replace("\n", " ").strip()
        if len(summary) > 120:
            summary = summary[:119] + "…"
        lines.append(
            f"[unit#{unit.get('idx')} ep={unit.get('episode_no')}] "
            f"hook={hook}; conflict={conflict}; payoff={payoff}; summary={summary}"
        )
    return "\n".join(lines) if lines else "(no plot units)"


def _build_episode_block(ctx: SignalContext, *, max_episodes: int = 12) -> str:
    bucket: dict[int, list[dict[str, Any]]] = {}
    for unit in ctx.plot_units:
        ep = unit.get("episode_no")
        if ep is None:
            continue
        bucket.setdefault(int(ep), []).append(unit)
    lines: list[str] = []
    for episode_no in sorted(bucket.keys())[:max_episodes]:
        episode_units = bucket[episode_no][:8]
        intensity = []
        for unit in episode_units:
            unit_id = str(unit.get("id") or "")
            dialogue_density = ctx.unit_value(unit_id, "dialogue_density", default="none")
            voiceover = ctx.unit_value(unit_id, "voiceover_type", default="none")
            payoff = ctx.unit_value(unit_id, "payoff_type", default="none")
            intensity.append(f"density={dialogue_density}/voice={voiceover}/payoff={payoff}")
        lines.append(f"[ep={episode_no}] " + "; ".join(intensity))
    return "\n".join(lines) if lines else "(no episode stats)"


def _bundle_context_block(bundle_scope: str, ctx: SignalContext) -> str:
    drama = ", ".join(ctx.drama_tags[:8]) if ctx.drama_tags else "none"
    header = (
        f"script_id={ctx.script_id}\n"
        f"title={ctx.script_meta.get('title') or ''}\n"
        f"episodes={ctx.episode_count}\n"
        f"plot_units={ctx.plot_unit_count}\n"
        f"drama_tags={drama}\n"
    )
    if bundle_scope == "episode":
        return header + "\n[episode_view]\n" + _build_episode_block(ctx)
    return header + "\n[script_view]\n" + _build_plot_unit_block(ctx)


def _default_signal_value(signal_key: str) -> SignalValue:
    return SignalValue(
        key=signal_key,
        value=None,
        score=None,
        source="llm",
        confidence=0.0,
        evidence_refs=[],
        meta={"fallback": True},
    )


async def compute_llm_signals(
    rubric: RubricConfig,
    ctx: SignalContext,
    *,
    caller: LlmCaller | None = None,
    seed: int = 42,
) -> dict[str, SignalValue]:
    out: dict[str, SignalValue] = {}
    llm_signal_keys = {signal.id for signal in rubric.list_signals() if signal.source in {"llm", "hybrid"}}
    if not llm_signal_keys:
        return out

    if caller is None:
        for signal_key in sorted(llm_signal_keys):
            out[signal_key] = _default_signal_value(signal_key)
        return out

    for bundle in rubric.llm_bundles:
        template = load_prompt_by_bundle(rubric.rubric_id, bundle.id)
        context_block = _bundle_context_block(bundle.scope, ctx)
        prompt = render_prompt(
            template,
            rubric_id=rubric.rubric_id,
            score_ver=rubric.score_ver,
            bundle_id=bundle.id,
            scope=bundle.scope,
            signals=list(bundle.signals),
            context_block=context_block,
        )

        tier = ModelTier.PRIMARY if bundle.scope == "script" else ModelTier.MINI
        prompt_ver = f"{rubric.score_ver}:{bundle.id}"
        try:
            resp = await caller.call_json_deterministic(
                prompt,
                tag_set_ver=rubric.rubric_id,
                prompt_ver=prompt_ver,
                dim=f"signal_bundle:{bundle.id}",
                seed=seed,
                tier=tier,
                max_tokens=TokenBudget.DECISION_AGGREGATE,
            )
            parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
            parsed_signals = parsed.get("signals", {})
            if not isinstance(parsed_signals, dict):
                parsed_signals = {}
            for signal_key in bundle.signals:
                payload = parsed_signals.get(signal_key, {})
                if not isinstance(payload, dict):
                    payload = {}
                score = _safe_float(payload.get("score"))
                if score is not None:
                    score = max(0.0, min(10.0, score))
                confidence = _safe_float(payload.get("confidence"), 0.65) or 0.65
                value = payload.get("value")
                evidence = payload.get("evidence")
                if not isinstance(evidence, list):
                    evidence = []
                out[signal_key] = SignalValue(
                    key=signal_key,
                    value=value,
                    score=score,
                    source="llm",
                    confidence=max(0.0, min(1.0, confidence)),
                    evidence_refs=[item for item in evidence if isinstance(item, dict)],
                    meta={"bundle_id": bundle.id, "model": resp.model, "provider": resp.provider},
                )
        except ScoreLLMError:
            for signal_key in bundle.signals:
                out.setdefault(signal_key, _default_signal_value(signal_key))

    for signal_key in llm_signal_keys:
        out.setdefault(signal_key, _default_signal_value(signal_key))
    return out
