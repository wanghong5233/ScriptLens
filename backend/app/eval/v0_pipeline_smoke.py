from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import text

from service.script_tools.v0_tag_pipeline import run_v0_tag_pipeline
from utils.database import engine as default_engine


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test v0 tag pipeline.")
    parser.add_argument("--script-id", default="", help="script id or exact title")
    parser.add_argument("--tag-set", default="v0.1.0")
    parser.add_argument("--seed", type=int, default=42)
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
    summary = await run_v0_tag_pipeline(
        script_ref,
        tag_set_ver=args.tag_set,
        seed=args.seed,
    )
    print(
        json.dumps(
            {
                "script_id": summary.script_id,
                "plot_unit_count": summary.plot_unit_count,
                "character_entity_count": summary.character_entity_count,
                "drama_tags_count": summary.drama_tags_count,
                "plot_tag_units_count": summary.plot_tag_units_count,
                "asr_tag_units_count": summary.asr_tag_units_count,
                "tag_set_ver": summary.tag_set_ver,
                "seed": summary.seed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()

