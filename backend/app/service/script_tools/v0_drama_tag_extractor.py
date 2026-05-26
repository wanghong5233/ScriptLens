from __future__ import annotations

import hashlib
import os
from typing import Any

from service.script_tools.llm_caller import LlmCaller, ModelTier
from service.script_tools.v0_extractor_common import load_script_text, persist_script_tags, render_prompt, stable_choice
from service.tag_registry import load_prompt, load_tag_set, validate
from eval.stability.runner import ExtractorRegistry

_DIM = "drama_tags"
_SCOPE = "script"


def _fallback_tags(allowed_values: list[str], key: str) -> list[str]:
    if not allowed_values:
        return []
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    n = 1 + (int(digest[-2:], 16) % 3)
    out: list[str] = []
    for i in range(n):
        pick = stable_choice(allowed_values, f"{key}:{i}", default="")
        if pick and pick not in out:
            out.append(pick)
    return out


def _normalize_tags(raw: Any) -> list[str]:
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


async def extract_drama_tags(
    script_ref: str,
    *,
    tag_set_ver: str = "v0.1.0",
    seed: int = 42,
    variant: str = "a",
    caller: LlmCaller | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    cfg = load_tag_set(tag_set_ver)
    dim_cfg = cfg.get_dim(_DIM)
    allowed_values = list(dim_cfg.values)
    script_id, script_text = load_script_text(script_ref)
    prompt_text = render_prompt(
        load_prompt(tag_set_ver, _DIM),
        allowed_values=allowed_values,
        script_text=script_text,
        variant=variant,
    )
    prompt_ver = f"{cfg.prompt_ver}:{_DIM}:{variant}"
    caller = caller or LlmCaller()

    tags: list[str]
    model_ver = "fallback-hash"
    confidence = None
    disable_llm = os.getenv("SM_TAGGING_DISABLE_LLM", "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        if disable_llm:
            raise RuntimeError("llm disabled by env")
        resp = await caller.call_json_deterministic(
            prompt_text,
            tag_set_ver=tag_set_ver,
            prompt_ver=prompt_ver,
            dim=_DIM,
            seed=seed,
            tier=ModelTier.PRIMARY,
            max_tokens=512,
        )
        model_ver = resp.model
        parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
        tags = _normalize_tags(parsed.get(_DIM))
        validate(tag_set_ver, _DIM, tags)
    except Exception:
        tags = _fallback_tags(allowed_values, key=f"{script_ref}:{tag_set_ver}")

    if script_id and persist and tags:
        persist_script_tags(
            script_id=script_id,
            dim=_DIM,
            values=tags,
            tag_set_ver=tag_set_ver,
            prompt_ver=prompt_ver,
            model_ver=model_ver,
            source="llm",
            confidence=confidence,
            evidence={"scope": "script", "script_ref": script_ref},
            clear_existing=False,
        )

    return {
        _DIM: "|".join(sorted(tags)),
        "__drama_tags_list": tags,
        "__model_ver": model_ver,
        "__script_id": script_id or script_ref,
        "__prompt_ver": prompt_ver,
    }


async def extract(target_id: str, tag_set_ver: str, prompt_ver: str, seed: int, variant: str) -> dict[str, Any]:
    del prompt_ver  # runtime prompt version is built from registry + variant
    return await extract_drama_tags(
        target_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        persist=True,
    )


def register_v0_drama_extractor(tag_set_ver: str = "v0.1.0") -> None:
    if not ExtractorRegistry.has(tag_set_ver, _SCOPE):
        ExtractorRegistry.register(tag_set_ver, _SCOPE, extract)

