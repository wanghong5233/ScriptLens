from __future__ import annotations

import argparse
import json
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate v0 regression report after v1 refactor.")
    parser.add_argument("--reports-root", default="D:/workspace/dcccloud/ScriptLens/eval/reports")
    parser.add_argument("--output", default="D:/workspace/dcccloud/ScriptLens/eval/reports/v0_regression_after_v1.md")
    parser.add_argument("--json-output", default="D:/workspace/dcccloud/ScriptLens/eval/reports/v0_regression_after_v1.json")
    return parser.parse_args()


def _load_reports(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for fp in sorted(path.glob("*.json")):
        payload = json.loads(fp.read_text(encoding="utf-8"))
        out[str(payload.get("dim", fp.stem))] = payload
    return out


def main() -> None:
    args = _parse_args()
    root = Path(args.reports_root)
    baseline = {}
    baseline.update(_load_reports(root / "v0_stability_dev_script_per_dim"))
    baseline.update(_load_reports(root / "v0_stability_dev_plot_unit_per_dim"))
    current = {}
    current.update(_load_reports(root / "v0_stability_dev_script_after_v1_per_dim"))
    current.update(_load_reports(root / "v0_stability_dev_plot_unit_after_v1_per_dim"))

    rows = []
    for dim in sorted(set(baseline.keys()) & set(current.keys())):
        old_alpha = float(baseline[dim].get("intra_alpha", 0.0))
        new_alpha = float(current[dim].get("intra_alpha", 0.0))
        rows.append(
            {
                "dim": dim,
                "baseline_intra_alpha": old_alpha,
                "current_intra_alpha": new_alpha,
                "delta": new_alpha - old_alpha,
            }
        )
    rows.sort(key=lambda x: x["delta"])

    lines = [
        "# v0 Regression After v1",
        "",
        "| dim | baseline_intra_alpha | current_intra_alpha | delta |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['dim']} | {row['baseline_intra_alpha']:.3f} | "
            f"{row['current_intra_alpha']:.3f} | {row['delta']:.3f} |"
        )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    json_out = Path(args.json_output)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(out), "json_output": str(json_out), "rows": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

