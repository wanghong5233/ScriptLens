from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import text

from service.script_tools.tag_pipeline import run_tag_pipeline
from service.script_tools.extractor_common import resolve_script_id
from utils.database import engine as default_engine


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test schema-driven tag pipeline.")
    parser.add_argument("--script-id", default="", help="script id or exact title")
    parser.add_argument("--tag-set", default="v2.0.0")
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


async def _main_async() -> None:
    args = _parse_args()
    script_ref = args.script_id.strip() or (_latest_script_id() or "")
    if not script_ref:
        raise RuntimeError("no scripts found for smoke test")

    summary = await run_tag_pipeline(
        script_ref,
        tag_set_ver=args.tag_set,
        seed=args.seed,
        variant=args.variant,
    )
    resolved_script_id = resolve_script_id(script_ref, engine=default_engine) or summary.script_id
    if not summary.bundle_runs:
        raise RuntimeError("tag pipeline produced empty bundle_runs; check tag_set config")

    print(
        json.dumps(
            {
                "script_ref": script_ref,
                "script_id": resolved_script_id,
                "tag_set_ver": summary.tag_set_ver,
                "seed": summary.seed,
                "variant": summary.variant,
                "plot_unit_count": summary.plot_unit_count,
                "character_entity_count": summary.character_entity_count,
                "relationship_count": summary.relationship_count,
                "bundle_runs": summary.bundle_runs,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
