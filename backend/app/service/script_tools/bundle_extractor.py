from __future__ import annotations

import json
import os
from collections import OrderedDict
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from eval.stability.runner import ExtractorRegistry
from service.script_tools.extractor_common import (
    CharacterContext,
    EpisodeContext,
    PlotUnitContext,
    RelationshipContext,
    load_character_context,
    load_episode_context,
    load_plot_unit_context,
    load_relationship_context,
    load_script_text,
    persist_episode_tags,
    persist_plot_unit_tags,
    persist_script_tags,
    render_prompt,
    stable_choice,
)
from service.script_tools.llm_caller import LlmCaller, ModelTier
from service.tag_registry import load_bundle, load_prompt_by_bundle, load_tag_set, validate
from utils.database import engine as default_engine

_CACHE_LIMIT = 2048
_PAYLOAD_CACHE: "OrderedDict[tuple[str, str, int, str, str], dict[str, Any]]" = OrderedDict()


def _cache_get(key: tuple[str, str, int, str, str]) -> dict[str, Any] | None:
    payload = _PAYLOAD_CACHE.get(key)
    if payload is None:
        return None
    _PAYLOAD_CACHE.move_to_end(key)
    return payload


def _cache_put(key: tuple[str, str, int, str, str], payload: dict[str, Any]) -> None:
    _PAYLOAD_CACHE[key] = payload
    _PAYLOAD_CACHE.move_to_end(key)
    while len(_PAYLOAD_CACHE) > _CACHE_LIMIT:
        _PAYLOAD_CACHE.popitem(last=False)


def _env_disable_cache() -> bool:
    return os.getenv("SM_STABILITY_DISABLE_CACHE", "").strip().lower() in {"1", "true", "yes", "on"}


def _fallback_single(values: list[str], key_seed: str, *, default: str = "none") -> str:
    if not values:
        return default
    return stable_choice(values, key_seed, default=default)


def _fallback_multi(values: list[str], key_seed: str, *, max_n: int = 3) -> list[str]:
    if not values:
        return []
    picked: list[str] = []
    n = min(max_n, max(1, len(values)))
    for i in range(n):
        candidate = stable_choice(values, f"{key_seed}:{i}", default="")
        if candidate and candidate not in picked:
            picked.append(candidate)
    return picked


def _as_single(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, str):
            return first.strip()
    return ""


def _as_multi(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if value and value not in out:
            out.append(value)
    return out


def _dialogue_density(context: PlotUnitContext) -> str:
    full_text = context.full_text or ""
    dialogue_text = context.dialogue_text or ""
    total = len(full_text.replace("\n", "").strip())
    dialogue = len(dialogue_text.replace("\n", "").strip())
    if total <= 0 or dialogue <= 0:
        return "none"
    ratio = dialogue / max(total, 1)
    if ratio >= 0.55:
        return "dense"
    if ratio >= 0.3:
        return "moderate"
    return "sparse"


def _voiceover_type(context: PlotUnitContext) -> str:
    text_lower = (context.full_text or "").lower()
    has_narrator = any(x in text_lower for x in ("旁白", "画外音", "内心独白", " os ", " vo ", "v.o", "o.s"))
    has_dialogue = bool((context.dialogue_text or "").strip())
    if has_narrator and has_dialogue:
        return "mixed"
    if has_narrator:
        return "narrator"
    if has_dialogue:
        return "character"
    return "none"


def _compute_rule_override(dim: str, context: PlotUnitContext | None) -> str:
    if context is None:
        return "none"
    if dim == "dialogue_density":
        return _dialogue_density(context)
    if dim == "voiceover_type":
        return _voiceover_type(context)
    return "none"


def _dim_values(tag_set_ver: str, dims: tuple[str, ...]) -> dict[str, list[str]]:
    cfg = load_tag_set(tag_set_ver)
    return {dim: list(cfg.get_dim(dim).values) for dim in dims}


def _build_dims_meta(tag_set_ver: str, dims: tuple[str, ...]) -> list[dict[str, Any]]:
    cfg = load_tag_set(tag_set_ver)
    out: list[dict[str, Any]] = []
    for dim in dims:
        dim_cfg = cfg.get_dim(dim)
        out.append(
            {
                "dim": dim_cfg.dim,
                "scope": dim_cfg.scope,
                "cardinality": dim_cfg.cardinality,
                "open_enum": dim_cfg.open_enum,
                "stability_state": dim_cfg.stability_state,
                "values": list(dim_cfg.values),
            }
        )
    return out


def _normalize_bundle_values(
    *,
    raw: dict[str, Any],
    tag_set_ver: str,
    bundle_id: str,
    target_key: str,
    plot_context: PlotUnitContext | None,
) -> dict[str, str | list[str]]:
    bundle = load_bundle(tag_set_ver, bundle_id)
    cfg = load_tag_set(tag_set_ver)
    dim_values = _dim_values(tag_set_ver, bundle.dims)
    out: dict[str, str | list[str]] = {}
    rule_overrides = bundle.rule_overrides or {}

    for dim in bundle.dims:
        dim_cfg = cfg.get_dim(dim)
        key_seed = f"{target_key}:{bundle_id}:{dim}"
        if rule_overrides.get(dim) == "rule":
            raw_value = _compute_rule_override(dim, plot_context)
        else:
            raw_value = raw.get(dim)
        if dim_cfg.cardinality == "multi":
            values = _as_multi(raw_value)
            if not values:
                values = _fallback_multi(dim_values.get(dim, []), key_seed)
            try:
                validate(tag_set_ver, dim, values)
            except Exception:
                values = _fallback_multi(dim_values.get(dim, []), f"{key_seed}:retry")
            out[dim] = values
            continue

        value = _as_single(raw_value)
        if not value:
            value = _fallback_single(dim_values.get(dim, []), key_seed, default="none")
        try:
            validate(tag_set_ver, dim, value)
        except Exception:
            value = _fallback_single(dim_values.get(dim, []), f"{key_seed}:retry", default="none")
        out[dim] = value
    return out


def _build_plot_text(context: PlotUnitContext) -> str:
    return (
        f"summary:\n{context.summary}\n\n"
        f"action:\n{context.action_text[:1200]}\n\n"
        f"dialogue:\n{context.dialogue_text[:1200]}\n\n"
        f"prev_summary:\n{context.prev_summary}\n\n"
        f"next_summary:\n{context.next_summary}"
    )


def _render_bundle_prompt(
    *,
    tag_set_ver: str,
    bundle_id: str,
    variant: str,
    script_text: str = "",
    plot_text: str = "",
    dialogue_text: str = "",
    episode_text: str = "",
    character_text: str = "",
    relationship_text: str = "",
) -> str:
    bundle = load_bundle(tag_set_ver, bundle_id)
    dims_meta = _build_dims_meta(tag_set_ver, bundle.dims)
    dim_values = {item["dim"]: item["values"] for item in dims_meta}
    template_text = load_prompt_by_bundle(tag_set_ver, bundle_id)
    return render_prompt(
        template_text,
        dims=dims_meta,
        dim_values=dim_values,
        allowed_values=list(dim_values.get("drama_tags") or []),
        script_text=script_text,
        plot_unit_text=plot_text,
        dialogue_text=dialogue_text or plot_text,
        episode_text=episode_text,
        character_text=character_text,
        relationship_text=relationship_text,
        variant=variant,
    )


def _persist_character_values(
    *,
    context: CharacterContext,
    values: dict[str, str | list[str]],
    tag_set_ver: str,
    source: str,
    engine: Engine,
) -> None:
    archetype = str(values.get("character_archetype") or "") or None
    arc_type = str(values.get("character_arc_type") or "") or None
    agency = str(values.get("character_agency_level") or "") or None
    role_in_arc = str(values.get("character_role_in_arc") or "")
    evidence = {
        "character_role_in_arc": role_in_arc,
        "canonical_name": context.canonical_name,
        "aliases": context.aliases,
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE scriptlens.character_entities
                SET archetype = :archetype,
                    arc_type = :arc_type,
                    agency_level = :agency_level,
                    tag_set_ver = :tag_set_ver,
                    source = :source,
                    evidence = COALESCE(evidence, '{}'::jsonb) || CAST(:evidence AS jsonb)
                WHERE id = :cid
                """
            ),
            {
                "cid": context.character_id,
                "archetype": archetype,
                "arc_type": arc_type,
                "agency_level": agency,
                "tag_set_ver": tag_set_ver,
                "source": source,
                "evidence": json.dumps(evidence, ensure_ascii=False),
            },
        )


def _persist_relationship_values(
    *,
    context: RelationshipContext,
    values: dict[str, str | list[str]],
    tag_set_ver: str,
    source: str,
    engine: Engine,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE scriptlens.character_relationships
                SET relationship_type = :relationship_type,
                    polarity = :polarity,
                    dynamic_arc = :dynamic_arc,
                    triangle = :triangle,
                    tag_set_ver = :tag_set_ver,
                    source = :source,
                    evidence = COALESCE(evidence, '{}'::jsonb) || CAST(:evidence AS jsonb)
                WHERE id = :rid
                """
            ),
            {
                "rid": context.relationship_id,
                "relationship_type": str(values.get("relationship_type") or "") or None,
                "polarity": str(values.get("relationship_polarity") or "") or None,
                "dynamic_arc": str(values.get("relationship_dynamic_arc") or "") or None,
                "triangle": str(values.get("relationship_triangle") or "") or None,
                "tag_set_ver": tag_set_ver,
                "source": source,
                "evidence": json.dumps(
                    {
                        "src_name": context.src_name,
                        "dst_name": context.dst_name,
                    },
                    ensure_ascii=False,
                ),
            },
        )


async def extract_bundle(
    bundle_id: str,
    target_id: str,
    *,
    tag_set_ver: str,
    seed: int = 42,
    variant: str = "a",
    caller: LlmCaller | None = None,
    persist: bool = True,
    use_cache: bool = True,
    engine: Engine = default_engine,
) -> dict[str, Any]:
    cache_key = (bundle_id, target_id, seed, variant, tag_set_ver)
    cache_enabled = bool(use_cache) and not _env_disable_cache()
    if cache_enabled:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    cfg = load_tag_set(tag_set_ver)
    bundle = cfg.get_bundle(bundle_id)
    source = "llm"
    model_ver = "fallback-hash"
    target_key = target_id

    script_id: str | None = None
    plot_context: PlotUnitContext | None = None
    episode_context: EpisodeContext | None = None
    character_context: CharacterContext | None = None
    relationship_context: RelationshipContext | None = None
    prompt_text = ""

    if bundle.scope == "script":
        script_id, script_text = load_script_text(target_id, engine=engine)
        target_key = script_id or target_id
        prompt_text = _render_bundle_prompt(
            tag_set_ver=tag_set_ver,
            bundle_id=bundle_id,
            variant=variant,
            script_text=script_text,
        )
    elif bundle.scope == "plot_unit":
        plot_context = load_plot_unit_context(target_id, engine=engine)
        if plot_context is None:
            target_key = target_id
        else:
            target_key = plot_context.plot_unit_id
        prompt_text = _render_bundle_prompt(
            tag_set_ver=tag_set_ver,
            bundle_id=bundle_id,
            variant=variant,
            plot_text=_build_plot_text(plot_context) if plot_context else "",
            dialogue_text=((plot_context.dialogue_text or plot_context.full_text)[:1800] if plot_context else ""),
        )
    elif bundle.scope == "episode":
        episode_context = load_episode_context(target_id, engine=engine)
        if episode_context:
            target_key = f"{episode_context.script_id}::ep::{episode_context.episode_no}"
        prompt_text = _render_bundle_prompt(
            tag_set_ver=tag_set_ver,
            bundle_id=bundle_id,
            variant=variant,
            episode_text=episode_context.episode_text if episode_context else "",
        )
    elif bundle.scope == "character":
        character_context = load_character_context(target_id, engine=engine)
        if character_context:
            target_key = character_context.character_id
        prompt_text = _render_bundle_prompt(
            tag_set_ver=tag_set_ver,
            bundle_id=bundle_id,
            variant=variant,
            character_text=character_context.character_text if character_context else "",
        )
    elif bundle.scope == "relationship":
        relationship_context = load_relationship_context(target_id, engine=engine)
        if relationship_context:
            target_key = relationship_context.relationship_id
        prompt_text = _render_bundle_prompt(
            tag_set_ver=tag_set_ver,
            bundle_id=bundle_id,
            variant=variant,
            relationship_text=relationship_context.relationship_text if relationship_context else "",
        )
    else:
        raise ValueError(f"unsupported bundle scope={bundle.scope!r}")

    disable_llm = os.getenv("SM_TAGGING_DISABLE_LLM", "").strip().lower() in {"1", "true", "yes", "on"}
    caller = caller or LlmCaller()
    raw: dict[str, Any] = {}
    try:
        if disable_llm:
            raise RuntimeError("llm disabled by env")
        resp = await caller.call_json_deterministic(
            prompt_text,
            tag_set_ver=tag_set_ver,
            prompt_ver=f"{cfg.prompt_ver}:{bundle.id}:{variant}",
            dim=f"bundle:{bundle.id}",
            seed=seed,
            tier=ModelTier.PRIMARY,
            max_tokens=1024 if len(bundle.dims) > 4 else 768,
            use_cache=cache_enabled,
        )
        raw = resp.parsed if isinstance(resp.parsed, dict) else {}
        model_ver = resp.model
    except Exception:
        raw = {}

    values = _normalize_bundle_values(
        raw=raw,
        tag_set_ver=tag_set_ver,
        bundle_id=bundle.id,
        target_key=target_key,
        plot_context=plot_context,
    )

    if persist:
        if bundle.scope == "script" and script_id:
            for dim in bundle.dims:
                raw_value = values.get(dim)
                value_list = raw_value if isinstance(raw_value, list) else [str(raw_value)] if raw_value else []
                if not value_list:
                    continue
                persist_script_tags(
                    script_id=script_id,
                    dim=dim,
                    values=[str(v) for v in value_list],
                    tag_set_ver=tag_set_ver,
                    prompt_ver=f"{cfg.prompt_ver}:{bundle.id}:{variant}",
                    model_ver=model_ver,
                    source=source,
                    confidence=None,
                    evidence={"bundle_id": bundle.id, "target_id": target_id},
                    clear_existing=False,
                    engine=engine,
                )
        elif bundle.scope == "plot_unit" and plot_context:
            evidence = {dim: {"bundle_id": bundle.id, "target_id": target_id} for dim in values.keys()}
            persist_plot_unit_tags(
                plot_unit_id=plot_context.plot_unit_id,
                values_by_dim=values,
                tag_set_ver=tag_set_ver,
                prompt_ver=f"{cfg.prompt_ver}:{bundle.id}:{variant}",
                model_ver=model_ver,
                source=source,
                confidence=None,
                evidence_by_dim=evidence,
                clear_existing=False,
                engine=engine,
            )
        elif bundle.scope == "episode" and episode_context:
            evidence = {dim: {"bundle_id": bundle.id, "target_id": target_id} for dim in values.keys()}
            persist_episode_tags(
                script_id=episode_context.script_id,
                episode_no=episode_context.episode_no,
                values_by_dim=values,
                tag_set_ver=tag_set_ver,
                prompt_ver=f"{cfg.prompt_ver}:{bundle.id}:{variant}",
                model_ver=model_ver,
                source=source,
                confidence=None,
                evidence_by_dim=evidence,
                clear_existing=False,
                engine=engine,
            )
        elif bundle.scope == "character" and character_context:
            _persist_character_values(
                context=character_context,
                values=values,
                tag_set_ver=tag_set_ver,
                source=source,
                engine=engine,
            )
        elif bundle.scope == "relationship" and relationship_context:
            _persist_relationship_values(
                context=relationship_context,
                values=values,
                tag_set_ver=tag_set_ver,
                source=source,
                engine=engine,
            )

    payload = {
        **values,
        "__bundle_id": bundle.id,
        "__scope": bundle.scope,
        "__model_ver": model_ver,
    }
    if cache_enabled:
        _cache_put(cache_key, payload)
    return payload


def _dim_from_prompt_ver(prompt_ver: str) -> str:
    parts = (prompt_ver or "").split(":")
    return parts[1].strip() if len(parts) >= 2 else ""


async def extract_by_scope(
    target_id: str,
    tag_set_ver: str,
    prompt_ver: str,
    seed: int,
    variant: str,
    use_cache: bool = True,
) -> dict[str, Any]:
    cfg = load_tag_set(tag_set_ver)
    dim = _dim_from_prompt_ver(prompt_ver)
    bundle_id = cfg.dim_to_bundle_id.get(dim)
    if not bundle_id:
        raise KeyError(f"cannot resolve bundle for dim={dim!r} tag_set={tag_set_ver}")
    return await extract_bundle(
        bundle_id,
        target_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        use_cache=use_cache,
        persist=True,
    )


def register_bundle_scope_extractors(tag_set_ver: str) -> None:
    cfg = load_tag_set(tag_set_ver)
    for scope in cfg.scope_to_dims.keys():
        if not ExtractorRegistry.has(tag_set_ver, scope):
            ExtractorRegistry.register(tag_set_ver, scope, extract_by_scope)

