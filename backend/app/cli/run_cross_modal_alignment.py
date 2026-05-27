from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from service.script_tools.tag_alignment_analyzer import (
    DEFAULT_TARGET_DIMS,
    build_alignment_entries,
    load_script_side,
    load_video_side,
    render_json_deliverable,
    render_markdown_report,
    render_match_config,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build cross-modal shared-tag gates from script/video stability results.")
    parser.add_argument(
        "--script-stability-dir",
        action="append",
        required=True,
        help="Directory that contains per-dim stability json files (*.json). Can be passed multiple times.",
    )
    parser.add_argument(
        "--video-snapshot",
        default="service/match_registry/video_side_stability.yaml",
        help="Video-side stability snapshot yaml.",
    )
    parser.add_argument(
        "--out-config",
        default="service/script_tools/match_config.py",
        help="Output path for generated match_config.py",
    )
    parser.add_argument(
        "--out-md",
        default="eval/deliverables/reports/v0_cross_modal_tag_alignment.md",
        help="Output markdown report path.",
    )
    parser.add_argument(
        "--out-json",
        default="eval/deliverables/v0_cross_modal_tag_alignment.json",
        help="Output json deliverable path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print summary payload, do not write files.",
    )
    return parser.parse_args()


def _summary_payload(entries: list[Any], dry_run: bool) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    gate_count: dict[str, int] = {}
    for entry in entries:
        rows.append(
            {
                "dim": entry.dim,
                "scope": entry.scope,
                "tag_set_ver": entry.tag_set_ver,
                "script_verdict": entry.script_verdict,
                "video_verdict": entry.video_verdict,
                "gate": entry.gate.value,
                "reason": entry.reason,
            }
        )
        gate_count[entry.gate.value] = gate_count.get(entry.gate.value, 0) + 1
    return {
        "dry_run": dry_run,
        "gate_count": gate_count,
        "entries": rows,
    }


def main() -> None:
    args = _parse_args()
    target_dims = list(DEFAULT_TARGET_DIMS)
    dims = [meta["dim"] for meta in target_dims]
    script_reports = load_script_side([Path(p) for p in args.script_stability_dir], dims)
    video_entries = load_video_side(Path(args.video_snapshot))
    entries = build_alignment_entries(
        target_dims=tuple(target_dims),
        script_reports=script_reports,
        video_entries=video_entries,
    )

    summary = _summary_payload(entries, args.dry_run)
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    out_config = Path(args.out_config)
    out_md = Path(args.out_md)
    out_json = Path(args.out_json)
    render_match_config(entries, out_config)
    render_markdown_report(
        entries=entries,
        script_reports=script_reports,
        video_entries=video_entries,
        out_md=out_md,
    )
    render_json_deliverable(
        entries=entries,
        script_reports=script_reports,
        video_entries=video_entries,
        out_json=out_json,
    )
    summary["output_paths"] = {
        "config": str(out_config),
        "markdown": str(out_md),
        "json": str(out_json),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

