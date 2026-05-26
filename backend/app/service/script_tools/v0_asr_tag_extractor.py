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
from service.script_tools.v0_plot_tag_extractor import extract_plot_tags
from service.tag_registry import load_prompt, load_tag_set, validate

_SCOPE = "plot_unit"
_ASR_DIMS = (
    "dialogue_density",
    "speech_style",
    "cta_type",
    "voiceover_type",
    "emotional_keywords",
    "keyword_theme",
)
_RULE_DIMS = ("dialogue_density", "voiceover_type")
_LLM_DIMS = ("speech_style", "cta_type", "emotional_keywords", "keyword_theme")
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


def _asr_dim_values(tag_set_ver: str) -> dict[str, list[str]]:
    cfg = load_tag_set(tag_set_ver)
    return {dim: list(cfg.get_dim(dim).values) for dim in _ASR_DIMS}


def _dialogue_density(full_text: str, dialogue_text: str) -> str:
    total = len((full_text or "").replace("\n", "").strip())
    dialogue = len((dialogue_text or "").replace("\n", "").strip())
    if total <= 0 or dialogue <= 0:
        return "none"
    ratio = dialogue / max(total, 1)
    if ratio >= 0.55:
        return "dense"
    if ratio >= 0.3:
        return "moderate"
    return "sparse"


def _voiceover_type(full_text: str, dialogue_text: str) -> str:
    text_lower = (full_text or "").lower()
    has_narrator = any(x in text_lower for x in ("旁白", "画外音", "内心独白", " os ", " vo ", "v.o", "o.s"))
    has_dialogue = bool((dialogue_text or "").strip())
    if has_narrator and has_dialogue:
        return "mixed"
    if has_narrator:
        return "narrator"
    if has_dialogue:
        return "character"
    return "none"


def _normalize_values(raw: dict[str, Any], tag_set_ver: str, key_seed: str) -> dict[str, str]:
    values = _asr_dim_values(tag_set_ver)
    out: dict[str, str] = {}
    for dim in _LLM_DIMS:
        candidate = raw.get(dim)
        if not isinstance(candidate, str) or not candidate.strip():
            candidate = stable_choice(values.get(dim, []), f"{key_seed}:{dim}", default="none")
        candidate = candidate.strip()
        try:
            validate(tag_set_ver, dim, candidate)
        except Exception:
            candidate = stable_choice(values.get(dim, []), f"{key_seed}:{dim}:retry", default="none")
        out[dim] = candidate
    return out


async def extract_asr_tags(
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
    model_ver = "fallback-hash"
    if context is None:
        values = _normalize_values({}, tag_set_ver, key_seed=f"{target_id}:{tag_set_ver}")
        values["dialogue_density"] = "none"
        values["voiceover_type"] = "none"
        payload = {**values, "__model_ver": model_ver, "__plot_unit_id": target_id}
        _cache_put(cache_key, payload)
        return payload

    rule_values = {
        "dialogue_density": _dialogue_density(context.full_text, context.dialogue_text),
        "voiceover_type": _voiceover_type(context.full_text, context.dialogue_text),
    }
    for dim in _RULE_DIMS:
        try:
            validate(tag_set_ver, dim, rule_values[dim])
        except Exception:
            rule_values[dim] = "none"

    prompt_text = render_prompt(
        load_prompt(tag_set_ver, "speech_style"),
        dim_values=_asr_dim_values(tag_set_ver),
        dialogue_text=(context.dialogue_text or context.full_text)[:1800],
        variant=variant,
    )
    caller = caller or LlmCaller()
    disable_llm = os.getenv("SM_TAGGING_DISABLE_LLM", "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        if disable_llm:
            raise RuntimeError("llm disabled by env")
        resp = await caller.call_json_deterministic(
            prompt_text,
            tag_set_ver=tag_set_ver,
            prompt_ver=f"{cfg.prompt_ver}:asr_bundle:{variant}",
            dim="asr_bundle",
            seed=seed,
            tier=ModelTier.PRIMARY,
            max_tokens=512,
        )
        model_ver = resp.model
        parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
        llm_values = _normalize_values(parsed, tag_set_ver, key_seed=f"{context.plot_unit_id}:{tag_set_ver}")
    except Exception:
        llm_values = _normalize_values({}, tag_set_ver, key_seed=f"{context.plot_unit_id}:{tag_set_ver}:fallback")

    values = {**llm_values, **rule_values}
    if persist:
        evidence = {dim: {"target_id": target_id, "plot_unit_id": context.plot_unit_id} for dim in values.keys()}
        persist_plot_unit_tags(
            plot_unit_id=context.plot_unit_id,
            values_by_dim=values,
            tag_set_ver=tag_set_ver,
            prompt_ver=f"{cfg.prompt_ver}:asr_bundle:{variant}",
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
    # prompt_ver 里包含当前 dim，但 plot/asr 采用 bundle 调用；维度从 payload 取值即可。
    del prompt_ver
    plot_payload = await extract_plot_tags(
        target_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        persist=True,
    )
    asr_payload = await extract_asr_tags(
        target_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        persist=True,
    )
    merged = {**plot_payload, **asr_payload}
    if "__model_ver" not in merged:
        merged["__model_ver"] = asr_payload.get("__model_ver", plot_payload.get("__model_ver", "unknown"))
    return merged


def register_v0_plot_unit_extractor(tag_set_ver: str = "v0.1.0") -> None:
    if not ExtractorRegistry.has(tag_set_ver, _SCOPE):
        ExtractorRegistry.register(tag_set_ver, _SCOPE, extract)

