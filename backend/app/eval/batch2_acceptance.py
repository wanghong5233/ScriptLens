from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import text

from service.script_tools.script_ir import build_script_ir
from service.tag_registry.compat_check import check_tagset_compatibility
from utils.database import engine as default_engine


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch2 acceptance checker.")
    parser.add_argument("--v1-report-root", default="D:/workspace/dcccloud/ScriptLens/eval/reports")
    parser.add_argument("--output", default="D:/workspace/dcccloud/ScriptLens/eval/reports/batch2_acceptance.md")
    parser.add_argument(
        "--v1-yaml",
        default="D:/workspace/dcccloud/ScriptLens/backend/app/service/tag_registry/tag_sets/v1.yaml",
    )
    parser.add_argument("--alpha-threshold", type=float, default=0.7)
    parser.add_argument("--stable-ratio-threshold", type=float, default=0.6)
    parser.add_argument("--v0-regression-threshold", type=float, default=0.05)
    return parser.parse_args()


def _load_dim_reports(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    payload: dict[str, dict] = {}
    for fp in sorted(path.glob("*.json")):
        obj = json.loads(fp.read_text(encoding="utf-8"))
        payload[str(obj.get("dim", fp.stem))] = obj
    return payload


def _load_v1_reports(root: Path) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    suffixes = [
        "v1_stability_dev_script_per_dim",
        "v1_stability_dev_episode_per_dim",
        "v1_stability_dev_character_per_dim",
        "v1_stability_dev_relationship_per_dim",
    ]
    for suffix in suffixes:
        merged.update(_load_dim_reports(root / suffix))
    return merged


def _v0_regression(root: Path) -> tuple[float, dict[str, float]]:
    baseline = {}
    baseline.update(_load_dim_reports(root / "v0_stability_dev_script_per_dim"))
    baseline.update(_load_dim_reports(root / "v0_stability_dev_plot_unit_per_dim"))
    current = {}
    current.update(_load_dim_reports(root / "v0_stability_dev_script_after_v1_per_dim"))
    current.update(_load_dim_reports(root / "v0_stability_dev_plot_unit_after_v1_per_dim"))
    deltas: dict[str, float] = {}
    for dim, old in baseline.items():
        if dim not in current:
            continue
        old_alpha = float(old.get("intra_alpha", 0.0))
        new_alpha = float(current[dim].get("intra_alpha", 0.0))
        deltas[dim] = new_alpha - old_alpha
    worst_drop = min(deltas.values()) if deltas else 0.0
    return worst_drop, deltas


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


def _mvp_regression_ok() -> bool:
    sid = _latest_script_id()
    if not sid:
        return False
    ir = build_script_ir(sid, engine=default_engine)
    scene_count = sum(len(ep.scenes) for ep in ir.episodes)
    line_count = sum(len(scene.lines) for ep in ir.episodes for scene in ep.scenes)
    return scene_count > 0 and line_count > 0


def _update_v1_stability_state(v1_yaml_path: Path, reports: dict[str, dict], alpha_threshold: float) -> dict[str, str]:
    try:
        import yaml
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("pyyaml is required for batch2_acceptance") from exc

    if not v1_yaml_path.exists():
        return {}
    doc = yaml.safe_load(v1_yaml_path.read_text(encoding="utf-8")) or {}
    state_map: dict[str, str] = {}
    for scope, dims in (doc.get("scope") or {}).items():
        if not isinstance(dims, list):
            continue
        for dim_cfg in dims:
            if not isinstance(dim_cfg, dict):
                continue
            dim = str(dim_cfg.get("dim") or "").strip()
            if not dim:
                continue
            alpha = float((reports.get(dim) or {}).get("intra_alpha", 0.0))
            state = "stable" if alpha >= alpha_threshold else "proposed"
            dim_cfg["stability_state"] = state
            state_map[dim] = state
    v1_yaml_path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return state_map


def main() -> None:
    args = _parse_args()
    report_root = Path(args.v1_report_root)
    v1_reports = _load_v1_reports(report_root)
    v1_total = len(v1_reports)
    v1_stable = sum(1 for x in v1_reports.values() if float(x.get("intra_alpha", 0.0)) >= args.alpha_threshold)
    v1_ratio = (v1_stable / v1_total) if v1_total else 0.0

    worst_drop, deltas = _v0_regression(report_root)
    compat = check_tagset_compatibility("v0.1.0", "v1.0.0", mode="BACKWARD")
    mvp_ok = _mvp_regression_ok()
    updated_states = _update_v1_stability_state(Path(args.v1_yaml), v1_reports, args.alpha_threshold)

    checks = {
        "v1_dims_alpha_ge_threshold_ratio_ge_threshold": v1_ratio >= args.stable_ratio_threshold,
        "v0_regression_not_worse_than_threshold": worst_drop >= -abs(args.v0_regression_threshold),
        "compat_check_pass": compat.compatible,
        "mvp_regression_pass": mvp_ok,
    }
    all_pass = all(checks.values())

    lines = [
        "# Batch2 Acceptance",
        "",
        f"- v1 dims stable ratio: `{v1_stable}/{v1_total}` = `{v1_ratio:.3f}` "
        f"(threshold `>= {args.stable_ratio_threshold:.3f}`, alpha threshold `>= {args.alpha_threshold:.3f}`)",
        f"- v0 worst intra_alpha delta: `{worst_drop:.3f}` "
        f"(threshold `>= -{abs(args.v0_regression_threshold):.3f}`)",
        f"- compat(v0.1.0 -> v1.0.0, BACKWARD): `{'PASS' if compat.compatible else 'FAIL'}`",
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
            "## Stability State Update",
            "",
            f"- updated_dims: `{len(updated_states)}` (writeback to `{args.v1_yaml}`)",
            "",
            "## Overall",
            "",
            f"- result: `{'PASS' if all_pass else 'FAIL'}`",
        ]
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "v1_ratio": v1_ratio,
                "v0_worst_drop": worst_drop,
                "compat": compat.to_dict(),
                "checks": checks,
                "all_pass": all_pass,
                "updated_states": updated_states,
                "deltas": deltas,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

