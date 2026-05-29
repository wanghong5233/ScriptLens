from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.bundle_extractor import extract_bundle
from service.script_tools.character_entity_resolver import resolve_character_entities
from service.script_tools.extractor_common import resolve_script_id
from service.script_tools.llm_caller import LlmCaller
from service.script_tools.plot_unit_segmenter import segment_plot_units
from service.script_tools.relationship_candidate_generator import ensure_relationship_candidates
from service.script_tools.rule_extractors import persist_paid_break_positions_for_episodes
from service.tag_registry import list_bundles
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


def _resolve_pipeline_concurrency(default: int = 64) -> int:
    raw = os.getenv("SM_TAG_PIPELINE_CONCURRENCY", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


@dataclass
class PipelineRunSummary:
    script_id: str
    tag_set_ver: str
    seed: int
    variant: str
    plot_unit_count: int
    character_entity_count: int
    relationship_count: int
    bundle_runs: dict[str, int]


def _episode_targets(script_id: str, *, engine: Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT episode_no
                FROM scriptlens.scenes
                WHERE script_id = :sid AND episode_no IS NOT NULL
                ORDER BY episode_no
                LIMIT 80
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    return [f"{script_id}::ep::{int(row['episode_no'])}" for row in rows]


def _character_ids(script_id: str, *, engine: Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.character_entities
                WHERE script_id = :sid
                ORDER BY created_at, canonical_name
                LIMIT 300
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    return [str(row["id"]) for row in rows]


def _relationship_ids(script_id: str, *, tag_set_ver: str, engine: Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.character_relationships
                WHERE script_id = :sid
                  AND (tag_set_ver = :ver OR tag_set_ver = '' OR tag_set_ver IS NULL)
                ORDER BY created_at, id
                LIMIT 500
                """
            ),
            {"sid": script_id, "ver": tag_set_ver},
        ).mappings().all()
    return [str(row["id"]) for row in rows]


def _resolve_targets(
    scope: str,
    *,
    script_id: str,
    plot_unit_ids: list[str],
    character_ids: list[str],
    relationship_ids: list[str],
    episode_targets: list[str],
) -> list[str]:
    if scope == "script":
        return [script_id]
    if scope == "plot_unit":
        return list(plot_unit_ids)
    if scope == "character":
        return list(character_ids)
    if scope == "relationship":
        return list(relationship_ids)
    if scope == "episode":
        return list(episode_targets)
    raise ValueError(f"unsupported bundle scope={scope!r}")


async def run_tag_pipeline(
    script_ref: str,
    *,
    tag_set_ver: str = "script",
    seed: int = 42,
    variant: str = "a",
    caller: LlmCaller | None = None,
    engine: Engine = default_engine,
) -> PipelineRunSummary:
    script_id = resolve_script_id(script_ref, engine=engine) or script_ref
    caller = caller or LlmCaller()

    units = await segment_plot_units(
        script_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        caller=caller,
        persist=True,
        engine=engine,
    )
    entities = await resolve_character_entities(
        script_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        caller=caller,
        persist=True,
        engine=engine,
    )
    # relationship bundle targets are physical rows, so we refresh candidates first.
    ensure_relationship_candidates(
        script_id,
        tag_set_ver=tag_set_ver,
        min_cooccurrence=1,
        top_k=30,
        persist=True,
        engine=engine,
    )

    plot_unit_ids = [unit.id for unit in units]
    character_ids = _character_ids(script_id, engine=engine)
    relationship_ids = _relationship_ids(script_id, tag_set_ver=tag_set_ver, engine=engine)
    episode_targets = _episode_targets(script_id, engine=engine)

    concurrency = _resolve_pipeline_concurrency()
    sem = asyncio.Semaphore(concurrency)

    async def _bounded_extract(bundle_id: str, target_id: str) -> None:
        async with sem:
            await extract_bundle(
                bundle_id,
                target_id,
                tag_set_ver=tag_set_ver,
                seed=seed,
                variant=variant,
                caller=caller,
                persist=True,
                engine=engine,
            )

    bundle_runs: dict[str, int] = {}
    for bundle in list_bundles(tag_set_ver):
        targets = _resolve_targets(
            bundle.scope,
            script_id=script_id,
            plot_unit_ids=plot_unit_ids,
            character_ids=character_ids,
            relationship_ids=relationship_ids,
            episode_targets=episode_targets,
        )
        if not targets:
            bundle_runs[bundle.id] = 0
            continue
        # 同一 bundle 内跨 target 并发；不同 bundle 之间保持顺序（plot_unit/character/relationship
        # 的依赖已经在循环外解析完毕）。default concurrency=64，可由 SM_TAG_PIPELINE_CONCURRENCY 覆盖。
        logger.info(
            "tag_pipeline bundle=%s scope=%s targets=%d concurrency=%d",
            bundle.id, bundle.scope, len(targets), concurrency,
        )
        await asyncio.gather(*[_bounded_extract(bundle.id, target) for target in targets])
        bundle_runs[bundle.id] = len(targets)

    episode_nos = [int(x.rpartition("::ep::")[2]) for x in episode_targets if "::ep::" in x]
    if episode_nos:
        persist_paid_break_positions_for_episodes(
            script_id,
            episode_nos,
            tag_set_ver=tag_set_ver,
            prompt_ver=f"rule:paid_break_position:{variant}",
            engine=engine,
        )
    bundle_runs["rule_paid_break_position"] = len(episode_nos)

    return PipelineRunSummary(
        script_id=script_id,
        tag_set_ver=tag_set_ver,
        seed=seed,
        variant=variant,
        plot_unit_count=len(units),
        character_entity_count=len(entities),
        relationship_count=len(relationship_ids),
        bundle_runs=bundle_runs,
    )
