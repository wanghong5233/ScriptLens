from __future__ import annotations

from dataclasses import dataclass

from service.script_tools.character_entity_resolver import resolve_character_entities
from service.script_tools.plot_unit_segmenter import segment_plot_units
from service.script_tools.v0_asr_tag_extractor import extract_asr_tags
from service.script_tools.v0_drama_tag_extractor import extract_drama_tags
from service.script_tools.v0_extractor_common import resolve_script_id
from service.script_tools.v0_plot_tag_extractor import extract_plot_tags


@dataclass
class PipelineRunSummary:
    script_id: str
    plot_unit_count: int
    character_entity_count: int
    drama_tags_count: int
    plot_tag_units_count: int
    asr_tag_units_count: int
    tag_set_ver: str
    seed: int


async def run_v0_tag_pipeline(
    script_ref: str,
    *,
    tag_set_ver: str = "v0.1.0",
    seed: int = 42,
    variant: str = "a",
) -> PipelineRunSummary:
    script_id = resolve_script_id(script_ref) or script_ref
    units = await segment_plot_units(
        script_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        persist=True,
    )
    entities = await resolve_character_entities(
        script_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        persist=True,
    )
    drama_payload = await extract_drama_tags(
        script_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        persist=True,
    )
    drama_tags_list = drama_payload.get("__drama_tags_list") or []

    plot_unit_targets = [u.id for u in units]
    for target in plot_unit_targets:
        await extract_plot_tags(target, tag_set_ver=tag_set_ver, seed=seed, variant=variant, persist=True)
        await extract_asr_tags(target, tag_set_ver=tag_set_ver, seed=seed, variant=variant, persist=True)

    return PipelineRunSummary(
        script_id=script_id,
        plot_unit_count=len(units),
        character_entity_count=len(entities),
        drama_tags_count=len(drama_tags_list),
        plot_tag_units_count=len(plot_unit_targets),
        asr_tag_units_count=len(plot_unit_targets),
        tag_set_ver=tag_set_ver,
        seed=seed,
    )

