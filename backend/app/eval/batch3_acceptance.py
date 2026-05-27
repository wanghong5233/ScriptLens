from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.engine import Engine

from eval.run_scoring_stability import run_stability_eval
from eval.stability.sampler import sample_split
from eval.v3_scoring_smoke import _generate_report_dry_run, _scoring_tables_ready
from service.score_registry import check_rubric_compatibility, load_rubric
from service.script_report_service import generate_report
from service.script_tools.v0_extractor_common import resolve_script_id
from utils.database import engine as default_engine


def _latest_script_ids(*, limit: int, engine: Engine) -> list[str]:
    with engine.connect() as conn:
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
    return [str(row["id"]) for row in rows]


def _parse_script_refs(
    raw: str,
    *,
    limit: int,
    split: str,
    source_dir: str,
    engine: Engine,
) -> list[str]:
    refs = [item.strip() for item in raw.split(",") if item.strip()]
    if not refs:
        sampled = sample_split(split=split, n_scripts=limit, source_dir=source_dir)
        sampled_ids = [resolve_script_id(item.script_id, engine=engine) or item.script_id for item in sampled]
        sampled_ids = [str(item) for item in sampled_ids if str(item).strip()]
        if sampled_ids:
            out: list[str] = []
            seen: set[str] = set()
            for sid in sampled_ids:
                if sid in seen:
                    continue
                seen.add(sid)
                out.append(sid)
            return out
        return _latest_script_ids(limit=limit, engine=engine)
    out: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        sid = resolve_script_id(ref, engine=engine) or ref
        if sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def _action_score(actions: list[dict[str, Any]]) -> float:
    if not actions:
        return 0.0
    row_scores: list[float] = []
    for action in actions:
        checks = [
            bool(str(action.get("issue") or "").strip()),
            bool(str(action.get("target") or "").strip()),
            len(action.get("action_steps") or []) >= 2,
            len(action.get("evidence_refs") or []) >= 1,
            bool(action.get("estimated_lift")),
        ]
        row_scores.append(sum(1.0 for ok in checks if ok) / len(checks))
    return float(sum(row_scores) / len(row_scores)) if row_scores else 0.0


def _corr_matrix(
    reports: dict[str, dict[str, Any]],
    dims: list[str],
) -> tuple[list[list[float]], float]:
    rows: list[list[float]] = []
    for report in reports.values():
        score_map = {item.get("dimension"): item.get("score") for item in (report.get("scorecard") or [])}
        row = []
        for dim in dims:
            value = score_map.get(dim)
            row.append(float(value) if value is not None else 0.0)
        rows.append(row)
    if len(rows) <= 1:
        ident = [[1.0 if i == j else 0.0 for j in range(len(dims))] for i in range(len(dims))]
        return ident, 0.0
    matrix = np.array(rows, dtype=np.float64)
    corr = np.corrcoef(matrix, rowvar=False)
    corr = np.nan_to_num(corr, nan=0.0, posinf=1.0, neginf=-1.0)
    max_abs = 0.0
    for i in range(corr.shape[0]):
        for j in range(corr.shape[1]):
            if i == j:
                continue
            max_abs = max(max_abs, abs(float(corr[i][j])))
    return corr.tolist(), max_abs


def _coverage_summary(
    reports: dict[str, dict[str, Any]],
    dims: list[str],
) -> tuple[dict[str, float], int]:
    coverage_acc: dict[str, list[float]] = {dim: [] for dim in dims}
    insufficient_count = 0
    for report in reports.values():
        for item in report.get("scorecard") or []:
            dim = str(item.get("dimension") or "")
            if dim not in coverage_acc:
                continue
            ratio = item.get("coverage_ratio")
            if ratio is not None:
                coverage_acc[dim].append(float(ratio))
            if str(item.get("tier") or "") == "insufficient":
                insufficient_count += 1
    summary = {
        dim: (float(sum(values) / len(values)) if values else 0.0)
        for dim, values in coverage_acc.items()
    }
    return summary, insufficient_count


def _build_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Batch3 Acceptance",
        "",
        f"- scripts_evaluated: `{payload['scripts_evaluated']}`",
        f"- decoupling max |rho|: `{payload['decoupling']['max_abs_offdiag']:.4f}` "
        f"(threshold `< {payload['thresholds']['decoupling']:.4f}`)",
        f"- stability overall variance: `{payload['stability']['overall_variance']:.6f}` "
        f"(threshold `<= {payload['thresholds']['overall_variance']:.6f}`)",
        f"- actionability score: `{payload['actionability']['score']:.4f}` "
        f"(threshold `>= {payload['thresholds']['actionability']:.4f}`)",
        "",
        "## Checks",
        "",
    ]
    for name, ok in (payload.get("checks") or {}).items():
        lines.append(f"- {name}: `{'PASS' if ok else 'FAIL'}`")
    lines.extend(
        [
            "",
            "## Signal Coverage",
            "",
        ]
    )
    for dim, value in sorted((payload.get("signal_coverage") or {}).items()):
        lines.append(f"- `{dim}`: `{value:.4f}`")
    lines.append(f"- insufficient_dimensions: `{payload.get('insufficient_dimensions', 0)}`")
    lines.extend(
        [
            "",
            "## Overall",
            "",
            f"- result: `{'PASS' if payload.get('all_pass') else 'FAIL'}`",
        ]
    )
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch3 acceptance checks.")
    parser.add_argument("--script-ids", default="", help="comma-separated script ids or titles")
    parser.add_argument("--dev-limit", type=int, default=10)
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    parser.add_argument("--source-dir", default="../../eval/爆款短剧剧本（完整本）")
    parser.add_argument("--rubric-version", default="v3.0.0")
    parser.add_argument("--seeds", default="42,123,456,789,1011")
    parser.add_argument("--decoupling-threshold", type=float, default=0.6)
    parser.add_argument("--overall-variance-threshold", type=float, default=0.05)
    parser.add_argument("--dim-variance-threshold", type=float, default=0.1)
    parser.add_argument("--actionability-threshold", type=float, default=0.7)
    parser.add_argument("--actionability-sample-size", type=int, default=5)
    parser.add_argument(
        "--output",
        default="eval/deliverables/reports/batch3_acceptance.md",
        help="markdown output path",
    )
    parser.add_argument(
        "--json-output",
        default="eval/deliverables/reports/batch3_acceptance.json",
        help="json output path",
    )
    return parser.parse_args()


def _parse_seeds(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    if not out:
        raise ValueError("seeds must not be empty")
    return out


async def _main_async() -> None:
    args = _parse_args()
    script_ids = _parse_script_refs(
        args.script_ids,
        limit=args.dev_limit,
        split=args.split,
        source_dir=args.source_dir,
        engine=default_engine,
    )
    if not script_ids:
        raise RuntimeError("no scripts found for batch3 acceptance")

    rubric = load_rubric(args.rubric_version)
    reports: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    persist_ready = _scoring_tables_ready(default_engine)
    for script_id in script_ids:
        try:
            if persist_ready:
                reports[script_id] = await generate_report(script_id=script_id, engine=default_engine)
            else:
                reports[script_id] = await _generate_report_dry_run(
                    script_id=script_id,
                    rubric_version=args.rubric_version,
                    engine=default_engine,
                )
        except Exception as exc:  # noqa: BLE001
            errors[script_id] = f"{type(exc).__name__}: {exc}"

    dims = list(rubric.all_dimensions)
    corr_matrix, max_abs = _corr_matrix(reports, dims)
    stability_script = script_ids[0]
    stability = await run_stability_eval(
        script_id=stability_script,
        seeds=_parse_seeds(args.seeds),
        rubric_version=args.rubric_version,
        overall_variance_threshold=args.overall_variance_threshold,
        dim_variance_threshold=args.dim_variance_threshold,
        engine=default_engine,
    )
    actionability_reports = list(reports.values())[: max(1, args.actionability_sample_size)]
    action_scores = [
        _action_score((item.get("evaluation") or {}).get("rewrite_seeds") or [])
        for item in actionability_reports
    ]
    actionability_score = (
        float(sum(action_scores) / len(action_scores))
        if action_scores
        else 0.0
    )
    compat = check_rubric_compatibility(args.rubric_version, args.rubric_version, mode="BACKWARD")
    coverage_summary, insufficient_count = _coverage_summary(reports, dims)

    checks = {
        "dimension_decoupling_pass": max_abs < args.decoupling_threshold,
        "stability_overall_variance_pass": bool(stability.get("checks", {}).get("overall_variance_le_threshold")),
        "stability_dimension_variance_pass": bool(stability.get("checks", {}).get("dimension_variance_le_threshold")),
        "actionability_pass": actionability_score >= args.actionability_threshold,
        "rubric_compat_pass": bool(compat.compatible),
        "report_generation_pass": len(errors) == 0 and len(reports) > 0,
    }

    payload = {
        "rubric_version": args.rubric_version,
        "scripts_requested": script_ids,
        "scripts_evaluated": len(reports),
        "failed_scripts": errors,
        "thresholds": {
            "decoupling": args.decoupling_threshold,
            "overall_variance": args.overall_variance_threshold,
            "dimension_variance": args.dim_variance_threshold,
            "actionability": args.actionability_threshold,
        },
        "decoupling": {
            "dimensions": dims,
            "corr_matrix": corr_matrix,
            "max_abs_offdiag": max_abs,
        },
        "stability": stability,
        "actionability": {
            "score": actionability_score,
            "sample_size": len(action_scores),
            "per_script_scores": action_scores,
        },
        "rubric_compat": compat.to_dict(),
        "signal_coverage": coverage_summary,
        "insufficient_dimensions": insufficient_count,
        "checks": checks,
        "all_pass": all(checks.values()),
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_build_markdown(payload), encoding="utf-8")

    json_output = Path(args.json_output)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "output": str(output),
                "json_output": str(json_output),
                "all_pass": payload["all_pass"],
                "checks": checks,
                "scripts_evaluated": payload["scripts_evaluated"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
