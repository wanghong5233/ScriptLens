from __future__ import annotations

import argparse
import asyncio
import json
import os
from itertools import combinations
from pathlib import Path

from sqlalchemy import text

from service.script_tools.character_entity_resolver import resolve_character_entities
from service.script_tools.plot_unit_segmenter import segment_plot_units
from service.script_tools.script_ir import build_script_ir
from utils.database import engine as default_engine


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch1 acceptance checker.")
    parser.add_argument("--tag-set", default="v0.1.0")
    parser.add_argument("--output", default="D:/workspace/dcccloud/ScriptLens/eval/reports/batch1_acceptance.md")
    parser.add_argument("--script-limit", type=int, default=3)
    return parser.parse_args()


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
    return [str(r["id"]) for r in rows]


def _f1(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    p = inter / len(a)
    r = inter / len(b)
    if p + r == 0:
        return 0.0
    return 2 * p * r / (p + r)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


async def _segmenter_consistency(script_ids: list[str], tag_set_ver: str) -> float:
    seeds = (42, 123, 456, 789, 1011)
    per_script_scores: list[float] = []
    for sid in script_ids:
        by_seed: dict[int, set[str]] = {}
        for seed in seeds:
            units = await segment_plot_units(
                sid,
                tag_set_ver=tag_set_ver,
                seed=seed,
                variant="a",
                persist=False,
            )
            boundaries = {u.start_scene_id for u in units[1:] if u.start_scene_id}
            by_seed[seed] = boundaries
        pair_scores = [
            _f1(by_seed[a], by_seed[b]) for a, b in combinations(seeds, 2)
        ]
        per_script_scores.append(sum(pair_scores) / len(pair_scores) if pair_scores else 1.0)
    return sum(per_script_scores) / len(per_script_scores) if per_script_scores else 0.0


async def _resolver_consistency(script_ids: list[str], tag_set_ver: str) -> float:
    seeds = (42, 123, 456, 789, 1011)
    per_script_scores: list[float] = []
    for sid in script_ids:
        by_seed: dict[int, set[str]] = {}
        for seed in seeds:
            entities = await resolve_character_entities(
                sid,
                tag_set_ver=tag_set_ver,
                seed=seed,
                persist=False,
            )
            by_seed[seed] = {e.canonical_name for e in entities}
        pair_scores = [
            _jaccard(by_seed[a], by_seed[b]) for a, b in combinations(seeds, 2)
        ]
        per_script_scores.append(sum(pair_scores) / len(pair_scores) if pair_scores else 1.0)
    return sum(per_script_scores) / len(per_script_scores) if per_script_scores else 0.0


def _stable_ratio_from_reports() -> tuple[float, int, int]:
    report_dirs = [
        Path("D:/workspace/dcccloud/ScriptLens/eval/reports/v0_stability_dev_script_per_dim"),
        Path("D:/workspace/dcccloud/ScriptLens/eval/reports/v0_stability_dev_plot_unit_per_dim"),
    ]
    total = 0
    stable = 0
    for d in report_dirs:
        if not d.exists():
            continue
        for fp in d.glob("*.json"):
            payload = json.loads(fp.read_text(encoding="utf-8"))
            total += 1
            if float(payload.get("intra_alpha", 0.0)) >= 0.7:
                stable += 1
    ratio = (stable / total) if total else 0.0
    return ratio, stable, total


def _mvp_regression_ok(script_ids: list[str]) -> bool:
    if not script_ids:
        return False
    ir = build_script_ir(script_ids[0], engine=default_engine)
    scene_count = sum(len(ep.scenes) for ep in ir.episodes)
    line_count = sum(len(sc.lines) for ep in ir.episodes for sc in ep.scenes)
    return scene_count > 0 and line_count > 0


async def _main_async() -> None:
    args = _parse_args()
    script_ids = _latest_script_ids(args.script_limit)
    os.environ["SM_TAGGING_DISABLE_LLM"] = "1"

    segmenter_score = await _segmenter_consistency(script_ids, args.tag_set)
    resolver_score = await _resolver_consistency(script_ids, args.tag_set)
    stable_ratio, stable_count, total_dims = _stable_ratio_from_reports()
    mvp_ok = _mvp_regression_ok(script_ids)

    checks = {
        "segmenter_f1_ge_0_7": segmenter_score >= 0.7,
        "resolver_consistency_ge_0_85": resolver_score >= 0.85,
        "v0_dims_alpha_ge_0_7_ratio_ge_0_6": stable_ratio >= 0.6,
        "mvp_regression_pass": mvp_ok,
    }
    all_pass = all(checks.values())

    lines = [
        "# Batch1 Acceptance",
        "",
        f"- tag_set: `{args.tag_set}`",
        f"- scripts_checked: `{len(script_ids)}`",
        "",
        "## Metrics",
        "",
        f"- segmenter pair-wise boundary F1: `{segmenter_score:.3f}` (threshold `>= 0.700`)",
        f"- resolver pair-wise consistency: `{resolver_score:.3f}` (threshold `>= 0.850`)",
        f"- v0 stable ratio (intra_alpha >= 0.7): `{stable_count}/{total_dims}` = `{stable_ratio:.3f}` (threshold `>= 0.600`)",
        f"- MVP regression (`build_script_ir` smoke): `{'PASS' if mvp_ok else 'FAIL'}`",
        "",
        "## Checks",
        "",
    ]
    for key, ok in checks.items():
        lines.append(f"- {key}: `{'PASS' if ok else 'FAIL'}`")
    lines.extend(
        [
            "",
            f"## Overall",
            "",
            f"- result: `{'PASS' if all_pass else 'FAIL'}`",
            "- note: this acceptance run uses deterministic mode (`SM_TAGGING_DISABLE_LLM=1`) for reproducible CI/local validation.",
        ]
    )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(out),
                "segmenter_score": segmenter_score,
                "resolver_score": resolver_score,
                "stable_ratio": stable_ratio,
                "checks": checks,
                "all_pass": all_pass,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()

