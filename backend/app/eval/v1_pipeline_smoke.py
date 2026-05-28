from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import text

from service.script_tools.bundle_extractor import extract_bundle
from service.script_tools.character_entity_resolver import resolve_character_entities
from service.script_tools.extractor_common import resolve_script_id
from service.script_tools.plot_unit_segmenter import segment_plot_units
from service.script_tools.relationship_candidate_generator import ensure_relationship_candidates
from utils.database import engine as default_engine


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test v1 bundle extraction pipeline.")
    parser.add_argument("--script-id", default="", help="script id or exact title")
    parser.add_argument("--tag-set", default="v1.0.0")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--variant", default="a", choices=["a", "b", "c"])
    return parser.parse_args()


def _latest_script_id() -> str | None:
    with default_engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.scripts
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().first()
    return str(row["id"]) if row else None


def _episode_targets(script_id: str) -> list[str]:
    with default_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT episode_no
                FROM scriptlens.scenes
                WHERE script_id = :sid AND episode_no IS NOT NULL
                ORDER BY episode_no
                LIMIT 10
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    return [f"{script_id}::ep::{int(r['episode_no'])}" for r in rows]


def _character_ids(script_id: str) -> list[str]:
    with default_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.character_entities
                WHERE script_id = :sid
                ORDER BY created_at, canonical_name
                LIMIT 80
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    return [str(r["id"]) for r in rows]


def _relationship_ids(script_id: str, tag_set_ver: str) -> list[str]:
    with default_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.character_relationships
                WHERE script_id = :sid AND (tag_set_ver = :ver OR tag_set_ver = '')
                ORDER BY created_at, id
                LIMIT 120
                """
            ),
            {"sid": script_id, "ver": tag_set_ver},
        ).mappings().all()
    return [str(r["id"]) for r in rows]


async def _main_async() -> None:
    args = _parse_args()
    script_ref = args.script_id.strip() or (_latest_script_id() or "")
    if not script_ref:
        raise RuntimeError("no scripts found for smoke test")
    script_id = resolve_script_id(script_ref, engine=default_engine) or script_ref

    await segment_plot_units(script_id, tag_set_ver=args.tag_set, seed=args.seed, variant=args.variant, persist=True)
    await resolve_character_entities(script_id, tag_set_ver=args.tag_set, seed=args.seed, persist=True)
    ensure_relationship_candidates(script_id, tag_set_ver=args.tag_set, min_cooccurrence=1, top_k=30, persist=True)

    script_payload = await extract_bundle(
        "v1_script_structure",
        script_id,
        tag_set_ver=args.tag_set,
        seed=args.seed,
        variant=args.variant,
        persist=True,
    )
    episode_targets = _episode_targets(script_id)
    episode_count = 0
    for target in episode_targets:
        await extract_bundle(
            "v1_episode_structure",
            target,
            tag_set_ver=args.tag_set,
            seed=args.seed,
            variant=args.variant,
            persist=True,
        )
        episode_count += 1

    character_ids = _character_ids(script_id)
    character_count = 0
    for cid in character_ids:
        await extract_bundle(
            "v1_character_attrs",
            cid,
            tag_set_ver=args.tag_set,
            seed=args.seed,
            variant=args.variant,
            persist=True,
        )
        character_count += 1

    relationship_ids = _relationship_ids(script_id, args.tag_set)
    relationship_count = 0
    for rid in relationship_ids:
        await extract_bundle(
            "v1_relationship",
            rid,
            tag_set_ver=args.tag_set,
            seed=args.seed,
            variant=args.variant,
            persist=True,
        )
        relationship_count += 1

    print(
        json.dumps(
            {
                "script_id": script_id,
                "tag_set_ver": args.tag_set,
                "script_dims": [k for k in script_payload.keys() if not k.startswith("__")],
                "episode_targets": episode_count,
                "character_targets": character_count,
                "relationship_targets": relationship_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()

