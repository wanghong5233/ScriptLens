from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import text

from eval.stability.report import write_baseline_comparison
from service.script_tools.v0_business_rule_baseline import derive_business_tags
from utils.database import engine as default_engine


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate v0 deliverables from stability reports + DB.")
    parser.add_argument(
        "--reports-root",
        default="D:/workspace/dcccloud/ScriptLens/eval/reports",
        help="Directory containing v0 stability reports",
    )
    parser.add_argument(
        "--output-root",
        default="D:/workspace/dcccloud/ScriptLens/eval/deliverables/v0",
        help="Deliverable output directory",
    )
    parser.add_argument("--tag-set", default="v0.1.0")
    parser.add_argument("--split", default="dev")
    return parser.parse_args()


def _load_dim_report(path: Path) -> dict:
    if not path.exists():
        return {}
    data: dict[str, dict] = {}
    for fp in sorted(path.glob("*.json")):
        payload = json.loads(fp.read_text(encoding="utf-8"))
        data[payload["dim"]] = payload
    return data


def _build_tag_set_yaml(
    *,
    tag_set_ver: str,
    split: str,
    script_reports: dict[str, dict],
    plot_reports: dict[str, dict],
    output_path: Path,
) -> None:
    all_reports = {}
    all_reports.update(script_reports)
    all_reports.update(plot_reports)
    stable = sorted([d for d, r in all_reports.items() if r.get("verdict") == "online"])
    fix = sorted([d for d, r in all_reports.items() if r.get("verdict") == "fix"])
    offline = sorted([d for d, r in all_reports.items() if r.get("verdict") == "offline"])
    payload = {
        "tag_set_ver": tag_set_ver,
        "split": split,
        "source_tag_set_file": "ScriptLens/backend/app/service/tag_registry/tag_sets/v0.yaml",
        "stability_classification": {
            "stable_dims": stable,
            "fix_dims": fix,
            "offline_dims": offline,
        },
        "note": "Classification derived from v0 stability reports in current environment.",
    }
    try:
        import yaml

        output_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception:
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_plot_unit_tags_for_script(script_id: str, tag_set_ver: str) -> tuple[list[str], list[dict]]:
    with default_engine.connect() as conn:
        drama_rows = conn.execute(
            text(
                """
                SELECT DISTINCT value
                FROM scriptlens.script_tags
                WHERE script_id = :sid AND dim = 'drama_tags' AND tag_set_ver = :ver AND source = 'llm'
                ORDER BY value
                """
            ),
            {"sid": script_id, "ver": tag_set_ver},
        ).mappings().all()
        units = conn.execute(
            text(
                """
                SELECT id::text AS id, idx, summary, episode_no
                FROM scriptlens.plot_units
                WHERE script_id = :sid
                ORDER BY idx
                LIMIT 40
                """
            ),
            {"sid": script_id},
        ).mappings().all()

    drama_tags = [str(r["value"]) for r in drama_rows]
    unit_payloads: list[dict] = []
    for unit in units:
        with default_engine.connect() as conn:
            tag_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT ON (dim)
                           dim, value, confidence, evidence, created_at
                    FROM scriptlens.plot_unit_tags
                    WHERE plot_unit_id = :pid AND tag_set_ver = :ver AND source = 'llm'
                    ORDER BY dim, created_at DESC
                    """
                ),
                {"pid": unit["id"], "ver": tag_set_ver},
            ).mappings().all()
        tags = {str(r["dim"]): str(r["value"]) for r in tag_rows}
        evidence = {str(r["dim"]): (r.get("evidence") or {}) for r in tag_rows}
        unit_payloads.append(
            {
                "plot_unit_id": unit["id"],
                "idx": int(unit["idx"]),
                "episode_no": unit.get("episode_no"),
                "summary": str(unit.get("summary") or ""),
                "tags": tags,
                "evidence": evidence,
            }
        )
    return drama_tags, unit_payloads


def _build_sample_plot_units_json(*, tag_set_ver: str, output_path: Path) -> None:
    with default_engine.connect() as conn:
        scripts = conn.execute(
            text(
                """
                SELECT id::text AS id, title
                FROM scriptlens.scripts
                ORDER BY created_at DESC
                LIMIT 10
                """
            )
        ).mappings().all()
    payload_scripts: list[dict] = []
    for script in scripts:
        drama_tags, units = _load_plot_unit_tags_for_script(str(script["id"]), tag_set_ver)
        payload_scripts.append(
            {
                "script_id": str(script["id"]),
                "title": str(script.get("title") or ""),
                "drama_tags": drama_tags,
                "plot_units": units,
            }
        )
    output_path.write_text(
        json.dumps({"tag_set_ver": tag_set_ver, "scripts": payload_scripts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_rule_baseline_outputs(tag_set_ver: str) -> dict[str, dict[str, dict[str, str]]]:
    rows: list[dict] = []
    with default_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT plot_unit_id::text AS plot_unit_id, dim, value
                FROM scriptlens.plot_unit_tags
                WHERE tag_set_ver = :ver AND source = 'llm'
                  AND dim IN (
                    'plot_hook', 'conflict_type', 'payoff_type', 'emotional_driver',
                    'business_content_archetype', 'business_conflict_bucket',
                    'business_payoff_bucket', 'business_emotion_bucket'
                  )
                """
            ),
            {"ver": tag_set_ver},
        ).mappings().all()

    by_target: dict[str, dict[str, str]] = {}
    for row in rows:
        by_target.setdefault(str(row["plot_unit_id"]), {})[str(row["dim"])] = str(row["value"])

    dims = (
        "business_content_archetype",
        "business_conflict_bucket",
        "business_payoff_bucket",
        "business_emotion_bucket",
    )
    outputs: dict[str, dict[str, dict[str, str]]] = {d: {"llm": {}, "rule": {}} for d in dims}
    for target_id, tags in by_target.items():
        rule_tags = derive_business_tags(tags)
        for dim in dims:
            llm_value = tags.get(dim)
            rule_value = rule_tags.get(dim)
            if llm_value:
                outputs[dim]["llm"][target_id] = llm_value
            if rule_value:
                outputs[dim]["rule"][target_id] = rule_value
    return outputs


def _build_cross_modal_checklist(
    *,
    tag_set_ver: str,
    split: str,
    script_reports: dict[str, dict],
    plot_reports: dict[str, dict],
    baseline_markdown: str,
    output_path: Path,
) -> None:
    all_reports = {}
    all_reports.update(script_reports)
    all_reports.update(plot_reports)
    lines = [
        f"# Cross-Modal Checklist ({tag_set_ver} / {split})",
        "",
        "## Script-Side Stability Snapshot",
        "",
        "| dim | intra_alpha | inter_alpha | verdict | video_check_suggestion |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for dim in sorted(all_reports.keys()):
        r = all_reports[dim]
        lines.append(
            f"| {dim} | {float(r.get('intra_alpha', 0.0)):.3f} | {float(r.get('inter_alpha', 0.0)):.3f} | "
            f"{r.get('verdict', '-')} | "
            "video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |"
        )
    lines.extend(
        [
            "",
            "## Rule Baseline Comparison (business_*)",
            "",
            baseline_markdown,
            "",
            "## Notes",
            "",
            "- 当前输出来自本地可用脚本样本（scripts 表已有数据），可持续增量更新。",
            "- 当 script 侧与 video 侧都达标后，维度进入共享内核；单侧达标则保留单侧使用。",
        ]
    )
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    reports_root = Path(args.reports_root)
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    script_reports = _load_dim_report(reports_root / "v0_stability_dev_script_per_dim")
    plot_reports = _load_dim_report(reports_root / "v0_stability_dev_plot_unit_per_dim")

    _build_tag_set_yaml(
        tag_set_ver=args.tag_set,
        split=args.split,
        script_reports=script_reports,
        plot_reports=plot_reports,
        output_path=output_root / "tag_set_v0.yaml",
    )
    _build_sample_plot_units_json(tag_set_ver=args.tag_set, output_path=output_root / "sample_plot_units.json")

    baseline_outputs = _build_rule_baseline_outputs(args.tag_set)
    # write_baseline_comparison expects DimStabilityReport objects for verdict.
    from eval.stability.report import DimStabilityReport

    report_objects: dict[str, DimStabilityReport] = {}
    for dim, raw in {**script_reports, **plot_reports}.items():
        report_objects[dim] = DimStabilityReport(
            dim=dim,
            intra_alpha=float(raw.get("intra_alpha", 0.0)),
            inter_alpha=float(raw.get("inter_alpha", 0.0)),
            kappa_mean=float(raw.get("kappa_mean", 0.0)),
            par=float(raw.get("par", 0.0)),
            cosine=float(raw.get("cosine", 0.0)),
            macro_f1=raw.get("macro_f1"),
            verdict=str(raw.get("verdict", "-")),
            unstable_values=list(raw.get("unstable_values") or []),
            genre_sensitive=list(raw.get("genre_sensitive") or []),
            confusion=raw.get("confusion"),
        )
    baseline_markdown = write_baseline_comparison(
        reports=report_objects,
        rule_baseline_outputs=baseline_outputs,
        path=str(output_root / "v0_baseline_comparison.md"),
    )

    _build_cross_modal_checklist(
        tag_set_ver=args.tag_set,
        split=args.split,
        script_reports=script_reports,
        plot_reports=plot_reports,
        baseline_markdown=baseline_markdown,
        output_path=output_root / "cross_modal_checklist.md",
    )

    print(
        json.dumps(
            {
                "output_root": str(output_root),
                "files": [
                    str(output_root / "tag_set_v0.yaml"),
                    str(output_root / "sample_plot_units.json"),
                    str(output_root / "v0_baseline_comparison.md"),
                    str(output_root / "cross_modal_checklist.md"),
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

