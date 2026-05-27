from __future__ import annotations

from service.script_tools.signal_catalog import SignalContext, SignalValue, register_signal


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in keywords)


def _is_protagonist(role: str) -> bool:
    if not role:
        return False
    return _contains_any(role, ("protagonist", "lead", "主角", "男主", "女主"))


def _is_antagonist(role: str) -> bool:
    if not role:
        return False
    return _contains_any(role, ("antagonist", "villain", "反派", "对手", "反角"))


def _stage_values(ctx: SignalContext) -> list[str]:
    return [value for value in ctx.plot_values("story_stage") if value and value != "none"]


@register_signal("protagonist_identifiable", scope="script", source="rule", primary_dim="character")
def compute_protagonist_identifiable(ctx: SignalContext) -> SignalValue:
    entities = ctx.character_entities
    protagonist_entities = [
        entity for entity in entities if _is_protagonist(str(entity.get("role") or ""))
    ]
    if protagonist_entities:
        score = 10.0
    elif entities:
        score = 6.0
    else:
        score = 2.0
    return SignalValue(
        key="protagonist_identifiable",
        value={
            "entity_count": len(entities),
            "protagonist_count": len(protagonist_entities),
        },
        score=score,
        source="rule",
        confidence=0.8,
        evidence_refs=[{"character_id": str(entity.get("id"))} for entity in protagonist_entities[:3]],
    )


@register_signal("protagonist_agency", scope="script", source="rule", primary_dim="character")
def compute_protagonist_agency(ctx: SignalContext) -> SignalValue:
    protagonist_entities = [
        entity for entity in ctx.character_entities if _is_protagonist(str(entity.get("role") or ""))
    ]
    if not protagonist_entities:
        protagonist_entities = ctx.character_entities[:1]
    if not protagonist_entities:
        return SignalValue(
            key="protagonist_agency",
            value={"high_or_medium_ratio": 0.0},
            score=2.0,
            source="rule",
            confidence=0.2,
        )
    levels = [str(entity.get("agency_level") or "").lower() for entity in protagonist_entities]
    high_or_medium = sum(1 for level in levels if level in {"high", "medium"})
    ratio = high_or_medium / len(protagonist_entities)
    score = round(min(10.0, max(1.0, ratio * 10.0)), 2)
    return SignalValue(
        key="protagonist_agency",
        value={"high_or_medium_ratio": round(ratio, 4)},
        score=score,
        source="rule",
        confidence=0.75,
        evidence_refs=[{"character_id": str(entity.get("id"))} for entity in protagonist_entities[:3]],
    )


@register_signal("decision_setup_rate", scope="script", source="rule", primary_dim="character")
def compute_decision_setup_rate(ctx: SignalContext) -> SignalValue:
    stages = _stage_values(ctx)
    if not stages:
        return SignalValue(
            key="decision_setup_rate",
            value={"setup_ratio": 0.0},
            score=3.0,
            source="rule",
            confidence=0.3,
        )
    decision_stage_count = sum(1 for stage in stages if stage in {"trigger", "escalation", "climax"})
    setup_stage_count = sum(1 for stage in stages if stage in {"setup", "trigger"})
    ratio = setup_stage_count / max(decision_stage_count, 1)
    score = round(min(10.0, max(1.0, ratio * 8.0)), 2)
    return SignalValue(
        key="decision_setup_rate",
        value={"setup_ratio": round(ratio, 4)},
        score=score,
        source="rule",
        confidence=0.65,
    )


@register_signal("ooc_rate", scope="script", source="rule", primary_dim="character")
def compute_ooc_rate(ctx: SignalContext) -> SignalValue:
    agency_signal = compute_protagonist_agency(ctx)
    setup_signal = compute_decision_setup_rate(ctx)
    agency_score = agency_signal.score or 0.0
    setup_score = setup_signal.score or 0.0
    ooc_ratio = max(0.0, 1.0 - ((agency_score + setup_score) / 20.0))
    score = round(max(0.0, 10.0 - ooc_ratio * 10.0), 2)
    return SignalValue(
        key="ooc_rate",
        value={"ooc_ratio": round(ooc_ratio, 4)},
        score=score,
        source="rule",
        confidence=0.55,
    )


@register_signal("antagonist_quality", scope="script", source="rule", primary_dim="character")
def compute_antagonist_quality(ctx: SignalContext) -> SignalValue:
    antagonists = [
        entity for entity in ctx.character_entities if _is_antagonist(str(entity.get("role") or ""))
    ]
    negative_relationships = [
        rel
        for rel in ctx.character_relationships
        if str(rel.get("polarity") or "").lower() in {"negative", "unstable"}
    ]
    if antagonists and negative_relationships:
        score = 9.0
    elif antagonists:
        score = 7.0
    elif negative_relationships:
        score = 6.0
    elif ctx.character_entities:
        score = 4.0
    else:
        score = 2.0
    return SignalValue(
        key="antagonist_quality",
        value={
            "antagonist_count": len(antagonists),
            "negative_relationship_count": len(negative_relationships),
        },
        score=score,
        source="rule",
        confidence=0.75,
    )


@register_signal("relationship_conflict_strength", scope="script", source="rule", primary_dim="character")
def compute_relationship_conflict_strength(ctx: SignalContext) -> SignalValue:
    relationships = ctx.character_relationships
    if not relationships:
        return SignalValue(
            key="relationship_conflict_strength",
            value={"conflict_ratio": 0.0},
            score=3.0,
            source="rule",
            confidence=0.3,
        )
    conflict_count = sum(
        1
        for rel in relationships
        if str(rel.get("polarity") or "").lower() in {"negative", "mixed", "unstable"}
    )
    ratio = conflict_count / len(relationships)
    score = round(min(10.0, max(1.0, ratio * 10.0)), 2)
    return SignalValue(
        key="relationship_conflict_strength",
        value={"conflict_ratio": round(ratio, 4)},
        score=score,
        source="rule",
        confidence=0.8,
        evidence_refs=[{"relationship_id": str(rel.get("id"))} for rel in relationships[:4]],
    )


@register_signal("relationship_arc_change", scope="script", source="rule", primary_dim="character")
def compute_relationship_arc_change(ctx: SignalContext) -> SignalValue:
    relationships = ctx.character_relationships
    if not relationships:
        return SignalValue(
            key="relationship_arc_change",
            value={"dynamic_ratio": 0.0},
            score=3.0,
            source="rule",
            confidence=0.3,
        )
    dynamic_count = sum(
        1
        for rel in relationships
        if str(rel.get("dynamic_arc") or "").strip().lower() not in {"", "none"}
    )
    ratio = dynamic_count / len(relationships)
    score = round(min(10.0, max(1.0, ratio * 9.0 + 1.0)), 2)
    return SignalValue(
        key="relationship_arc_change",
        value={"dynamic_ratio": round(ratio, 4)},
        score=score,
        source="rule",
        confidence=0.7,
    )
