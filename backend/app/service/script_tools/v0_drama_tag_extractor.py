from __future__ import annotations

from typing import Any

from eval.stability.runner import ExtractorRegistry
from service.script_tools.bundle_extractor import extract_bundle

_DIM = "drama_tags"
_SCOPE = "script"


async def extract_drama_tags(
    script_ref: str,
    *,
    tag_set_ver: str = "v0.1.0",
    seed: int = 42,
    variant: str = "a",
    caller=None,  # kept for backward-compatible signature
    persist: bool = True,
) -> dict[str, Any]:
    payload = await extract_bundle(
        "v0_drama",
        script_ref,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        caller=caller,
        persist=persist,
    )
    raw_tags = payload.get(_DIM)
    tags = [str(v) for v in raw_tags] if isinstance(raw_tags, list) else []
    return {
        _DIM: "|".join(sorted(tags)),
        "__drama_tags_list": tags,
        "__model_ver": str(payload.get("__model_ver", "unknown")),
        "__script_id": script_ref,
        "__prompt_ver": f"{tag_set_ver}:{_DIM}:{variant}",
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

