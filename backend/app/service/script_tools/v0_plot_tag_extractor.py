from __future__ import annotations

from collections import OrderedDict
import os
from typing import Any

from eval.stability.runner import ExtractorRegistry
from service.script_tools.llm_caller import LlmCaller, ModelTier
from service.script_tools.v0_extractor_common import (
    load_plot_unit_context,
    persist_plot_unit_tags,
    render_prompt,
    stable_choice,
)
from service.tag_registry import load_prompt, load_tag_set, validate

_SCOPE = "plot_unit"
_PLOT_DIMS = (
    "plot_hook",
    "conflict_type",
    "story_stage",
    "relationship_arc",
    "payoff_type",
    "emotional_driver",
    "business_content_archetype",
    "business_conflict_bucket",
    "business_payoff_bucket",
    "business_emotion_bucket",
)
_CACHE_LIMIT = 1024
_PAYLOAD_CACHE: "OrderedDict[tuple[str, int, str, str], dict[str, Any]]" = OrderedDict()


def _cache_get(key: tuple[str, int, str, str]) -> dict[str, Any] | None:
    payload = _PAYLOAD_CACHE.get(key)
    if payload is None:
        return None
    _PAYLOAD_CACHE.move_to_end(key)
    return payload


def _cache_put(key: tuple[str, int, str, str], payload: dict[str, Any]) -> None:
    _PAYLOAD_CACHE[key] = payload
    _PAYLOAD_CACHE.move_to_end(key)
    while len(_PAYLOAD_CACHE) > _CACHE_LIMIT:
        _PAYLOAD_CACHE.popitem(last=False)


def _dim_values(tag_set_ver: str) -> dict[str, list[str]]:
    cfg = load_tag_set(tag_set_ver)
    out: dict[str, list[str]] = {}
    for dim in _PLOT_DIMS:
        out[dim] = list(cfg.get_dim(dim).values)
    return out


def _normalize_values(raw: dict[str, Any], tag_set_ver: str, key_seed: str) -> dict[str, str]:
    dim_values = _dim_values(tag_set_ver)
    out: dict[str, str] = {}
    for dim in _PLOT_DIMS:
        candidate = raw.get(dim)
        if not isinstance(candidate, str) or not candidate.strip():
            candidate = stable_choice(dim_values.get(dim, []), f"{key_seed}:{dim}", default="none")
        candidate = candidate.strip()
        try:
            validate(tag_set_ver, dim, candidate)
        except Exception:
            candidate = stable_choice(dim_values.get(dim, []), f"{key_seed}:{dim}:retry", default="none")
        out[dim] = candidate
    return out


async def extract_plot_tags(
    target_id: str,
    *,
    tag_set_ver: str = "v0.1.0",
    seed: int = 42,
    variant: str = "a",
    caller: LlmCaller | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    cache_key = (target_id, seed, variant, tag_set_ver)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    context = load_plot_unit_context(target_id)
    cfg = load_tag_set(tag_set_ver)
    prompt_tpl = load_prompt(tag_set_ver, "plot_hook")
    model_ver = "fallback-hash"

    if context is None:
        values = _normalize_values({}, tag_set_ver, key_seed=f"{target_id}:{tag_set_ver}")
        payload = {**values, "__model_ver": model_ver, "__plot_unit_id": target_id}
        _cache_put(cache_key, payload)
        return payload

    caller = caller or LlmCaller()
    plot_text = (
        f"summary:\n{context.summary}\n\n"
        f"action:\n{context.action_text[:1200]}\n\n"
        f"dialogue:\n{context.dialogue_text[:1200]}\n\n"
        f"prev_summary:\n{context.prev_summary}\n\n"
        f"next_summary:\n{context.next_summary}"
    )
    prompt_text = render_prompt(
        prompt_tpl,
        dim_values=_dim_values(tag_set_ver),
        plot_unit_text=plot_text,
        variant=variant,
    )

    disable_llm = os.getenv("SM_TAGGING_DISABLE_LLM", "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        if disable_llm:
            raise RuntimeError("llm disabled by env")
        resp = await caller.call_json_deterministic(
            prompt_text,
            tag_set_ver=tag_set_ver,
            prompt_ver=f"{cfg.prompt_ver}:plot_bundle:{variant}",
            dim="plot_bundle",
            seed=seed,
            tier=ModelTier.PRIMARY,
            max_tokens=768,
        )
        model_ver = resp.model
        parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
        values = _normalize_values(parsed, tag_set_ver, key_seed=f"{context.plot_unit_id}:{tag_set_ver}")
    except Exception:
        values = _normalize_values({}, tag_set_ver, key_seed=f"{context.plot_unit_id}:{tag_set_ver}:fallback")

    if persist:
        evidence = {dim: {"target_id": target_id, "plot_unit_id": context.plot_unit_id} for dim in values.keys()}
        persist_plot_unit_tags(
            plot_unit_id=context.plot_unit_id,
            values_by_dim=values,
            tag_set_ver=tag_set_ver,
            prompt_ver=f"{cfg.prompt_ver}:plot_bundle:{variant}",
            model_ver=model_ver,
            source="llm",
            confidence=None,
            evidence_by_dim=evidence,
            clear_existing=False,
        )

    payload = {**values, "__model_ver": model_ver, "__plot_unit_id": context.plot_unit_id}
    _cache_put(cache_key, payload)
    return payload


async def extract(target_id: str, tag_set_ver: str, prompt_ver: str, seed: int, variant: str) -> dict[str, Any]:
    del prompt_ver
    return await extract_plot_tags(
        target_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        persist=True,
    )


def register_v0_plot_extractor(tag_set_ver: str = "v0.1.0") -> None:
    if not ExtractorRegistry.has(tag_set_ver, _SCOPE):
        ExtractorRegistry.register(tag_set_ver, _SCOPE, extract)

