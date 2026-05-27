from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import text

from service.script_tools.tag_pipeline import PipelineRunSummary, run_tag_pipeline
from utils.database import engine as default_engine


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run schema-driven tag pipeline for a script.")
    parser.add_argument("--script-id", default="", help="script id (UUID) or exact script title")
    parser.add_argument("--tag-set", required=True, help="tag set version, e.g. v2.0.0")
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


def _to_json(summary: PipelineRunSummary) -> str:
    payload = {
        "script_id": summary.script_id,
        "tag_set_ver": summary.tag_set_ver,
        "seed": summary.seed,
        "variant": summary.variant,
        "plot_unit_count": summary.plot_unit_count,
        "character_entity_count": summary.character_entity_count,
        "relationship_count": summary.relationship_count,
        "bundle_runs": summary.bundle_runs,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _main_async() -> None:
    args = _parse_args()
    script_ref = args.script_id.strip() or (_latest_script_id() or "")
    if not script_ref:
        raise RuntimeError("no script id found; pass --script-id explicitly")
    summary = await run_tag_pipeline(
        script_ref,
        tag_set_ver=args.tag_set,
        seed=args.seed,
        variant=args.variant,
    )
    print(_to_json(summary))


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
