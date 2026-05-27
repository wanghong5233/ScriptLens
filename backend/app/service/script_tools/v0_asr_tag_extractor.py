from __future__ import annotations

from typing import Any

from eval.stability.runner import ExtractorRegistry
from service.script_tools.bundle_extractor import extract_bundle
from service.script_tools.v0_plot_tag_extractor import extract_plot_tags

_SCOPE = "plot_unit"


async def extract_asr_tags(
    target_id: str,
    *,
    tag_set_ver: str = "v0.1.0",
    seed: int = 42,
    variant: str = "a",
    caller=None,  # kept for backward-compatible signature
    persist: bool = True,
) -> dict[str, Any]:
    return await extract_bundle(
        "v0_asr",
        target_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        caller=caller,
        persist=persist,
    )


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

