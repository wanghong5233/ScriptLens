from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import text

from utils.database import engine as default_engine


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate v1 deliverables from reports + DB.")
    parser.add_argument("--reports-root", default="D:/workspace/dcccloud/ScriptLens/eval/reports")
    parser.add_argument("--output-root", default="D:/workspace/dcccloud/ScriptLens/eval/deliverables/v1")
    parser.add_argument("--tag-set", default="v1.0.0")
    return parser.parse_args()


def _load_dim_reports(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    payload: dict[str, dict] = {}
    for fp in sorted(path.glob("*.json")):
        obj = json.loads(fp.read_text(encoding="utf-8"))
        payload[str(obj.get("dim", fp.stem))] = obj
    return payload


def _load_all_v1_reports(root: Path) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for name in [
        "v1_stability_dev_script_per_dim",
        "v1_stability_dev_episode_per_dim",
        "v1_stability_dev_character_per_dim",
        "v1_stability_dev_relationship_per_dim",
    ]:
        merged.update(_load_dim_reports(root / name))
    return merged


def _build_tag_set_yaml(*, tag_set_ver: str, reports: dict[str, dict], output_path: Path) -> None:
    stable = sorted([d for d, r in reports.items() if r.get("verdict") == "online"])
    fix = sorted([d for d, r in reports.items() if r.get("verdict") == "fix"])
    offline = sorted([d for d, r in reports.items() if r.get("verdict") == "offline"])
    payload = {
        "tag_set_ver": tag_set_ver,
        "stability_classification": {
            "stable_dims": stable,
            "fix_dims": fix,
            "offline_dims": offline,
        },
    }
    try:
        import yaml

        output_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception:
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sample_v1_artifacts(tag_set_ver: str) -> dict:
    with default_engine.connect() as conn:
        scripts = conn.execute(
            text(
                """
                SELECT id::text AS id, title
                FROM scriptlens.scripts
                ORDER BY created_at DESC
                LIMIT 5
                """
            )
        ).mappings().all()

    payload_scripts: list[dict] = []
    for script in scripts:
        sid = str(script["id"])
        with default_engine.connect() as conn:
            script_tags = conn.execute(
                text(
                    """
                    SELECT dim, value, created_at
                    FROM scriptlens.script_tags
                    WHERE script_id = :sid AND tag_set_ver = :ver
                    ORDER BY created_at DESC
                    """
                ),
                {"sid": sid, "ver": tag_set_ver},
            ).mappings().all()
            episode_tags = conn.execute(
                text(
                    """
                    SELECT episode_no, dim, value, created_at
                    FROM scriptlens.episode_tags
                    WHERE script_id = :sid AND tag_set_ver = :ver
                    ORDER BY episode_no, created_at DESC
                    """
                ),
                {"sid": sid, "ver": tag_set_ver},
            ).mappings().all()
            characters = conn.execute(
                text(
                    """
                    SELECT id::text AS id, canonical_name, role, archetype, arc_type, agency_level
                    FROM scriptlens.character_entities
                    WHERE script_id = :sid
                    ORDER BY created_at, canonical_name
                    LIMIT 20
                    """
                ),
                {"sid": sid},
            ).mappings().all()
            relationships = conn.execute(
                text(
                    """
                    SELECT id::text AS id, src_char_id::text AS src_char_id, dst_char_id::text AS dst_char_id,
                           relationship_type, polarity, dynamic_arc, triangle
                    FROM scriptlens.character_relationships
                    WHERE script_id = :sid AND (tag_set_ver = :ver OR tag_set_ver = '')
                    ORDER BY created_at, id
                    LIMIT 30
                    """
                ),
                {"sid": sid, "ver": tag_set_ver},
            ).mappings().all()

        def _serialize_rows(rows):
            out = []
            for row in rows:
                item = {}
                for k, v in dict(row).items():
                    if hasattr(v, "isoformat"):
                        item[k] = v.isoformat()
                    else:
                        item[k] = v
                out.append(item)
            return out

        payload_scripts.append(
            {
                "script_id": sid,
                "title": str(script.get("title") or ""),
                "script_tags": _serialize_rows(script_tags),
                "episode_tags": _serialize_rows(episode_tags),
                "characters": _serialize_rows(characters),
                "relationships": _serialize_rows(relationships),
            }
        )
    return {"tag_set_ver": tag_set_ver, "scripts": payload_scripts}


def main() -> None:
    args = _parse_args()
    reports_root = Path(args.reports_root)
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    reports = _load_all_v1_reports(reports_root)
    _build_tag_set_yaml(
        tag_set_ver=args.tag_set,
        reports=reports,
        output_path=output_root / "tag_set_v1.yaml",
    )

    sample_payload = _sample_v1_artifacts(args.tag_set)
    (output_root / "sample_v1_artifacts.json").write_text(
        json.dumps(sample_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Keep an explicit checklist of expected report files for handoff.
    report_paths = [
        "v1_stability_dev_script.md",
        "v1_stability_dev_episode.md",
        "v1_stability_dev_character.md",
        "v1_stability_dev_relationship.md",
        "v0_regression_after_v1.md",
    ]
    (output_root / "report_manifest.json").write_text(
        json.dumps({"reports": report_paths}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "output_root": str(output_root),
                "files": [
                    str(output_root / "tag_set_v1.yaml"),
                    str(output_root / "sample_v1_artifacts.json"),
                    str(output_root / "report_manifest.json"),
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

