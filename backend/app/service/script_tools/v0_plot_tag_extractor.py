from __future__ import annotations

from typing import Any

from eval.stability.runner import ExtractorRegistry
from service.script_tools.bundle_extractor import extract_bundle

_SCOPE = "plot_unit"


async def extract_plot_tags(
    target_id: str,
    *,
    tag_set_ver: str = "v0.1.0",
    seed: int = 42,
    variant: str = "a",
    caller=None,  # kept for backward-compatible signature
    persist: bool = True,
) -> dict[str, Any]:
    return await extract_bundle(
        "v0_plot",
        target_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        caller=caller,
        persist=persist,
    )


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

