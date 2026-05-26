from __future__ import annotations

import argparse
from collections import Counter

from sqlalchemy import text

from service.script_tools.script_ir import build_script_ir
from utils.database import engine as default_engine


def _latest_script_ids(limit: int) -> list[str]:
    with default_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.scripts
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
    return [r["id"] for r in rows]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test script_ir parser.")
    parser.add_argument("--script-ids", default="", help="Comma-separated script ids.")
    parser.add_argument("--limit", type=int, default=5, help="Fallback number of scripts when ids are omitted.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    script_ids = [x.strip() for x in args.script_ids.split(",") if x.strip()]
    if not script_ids:
        script_ids = _latest_script_ids(args.limit)

    if not script_ids:
        print("no scripts found")
        return

    for sid in script_ids:
        ir = build_script_ir(sid, engine=default_engine)
        kind_counter: Counter[str] = Counter()
        scene_count = 0
        line_count = 0
        for ep in ir.episodes:
            for sc in ep.scenes:
                scene_count += 1
                for line in sc.lines:
                    line_count += 1
                    kind_counter[line.kind] += 1
        print(
            f"script_id={sid} title={ir.title!r} episodes={len(ir.episodes)} "
            f"scenes={scene_count} lines={line_count} kinds={dict(kind_counter)}"
        )


if __name__ == "__main__":
    main()
