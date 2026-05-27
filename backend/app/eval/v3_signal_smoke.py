from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.score_registry import load_rubric
from service.script_report_service import ensure_v1_tags_ready
from service.script_tools.llm_caller import LlmCaller
from service.script_tools.signal_catalog import SignalValue, build_signal_context, compute_signals
from service.script_tools.v0_extractor_common import resolve_script_id
from utils.database import engine as default_engine


def _latest_script_id(engine: Engine) -> str | None:
    with engine.connect() as conn:
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


def _serialize_signal(signal: SignalValue) -> dict[str, Any]:
    return {
        "key": signal.key,
        "source": signal.source,
        "score": signal.score,
        "confidence": signal.confidence,
        "primary_dimension": signal.primary_dimension,
        "weight_in_dim": signal.weight_in_dim,
        "value": signal.value,
        "evidence_refs": signal.evidence_refs,
        "meta": signal.meta,
    }


def _build_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# V3 Signal Smoke",
        "",
        f"- script_id: `{payload['script_id']}`",
        f"- rubric_version: `{payload['rubric_version']}`",
        f"- total_signals: `{payload['total_signals']}`",
        f"- rule_signals: `{payload['source_counts'].get('rule', 0)}`",
        f"- llm_signals: `{payload['source_counts'].get('llm', 0)}`",
        "",
        "## Coverage by Dimension",
        "",
    ]
    for dim, item in sorted((payload.get("coverage_by_dimension") or {}).items()):
        lines.append(f"- `{dim}`: `{item['effective']}/{item['total']}` (`{item['ratio']:.3f}`)")
    lines.extend(
        [
            "",
            "## Signal Preview",
            "",
        ]
    )
    for item in payload.get("preview", []):
        lines.append(
            f"- `{item['key']}` ({item['source']}, dim={item.get('primary_dimension') or 'n/a'}): "
            f"score=`{item.get('score')}` confidence=`{item.get('confidence')}`"
        )
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch3 signal extraction smoke run.")
    parser.add_argument("--script-id", default="", help="script id or exact title")
    parser.add_argument("--rubric-version", default="v3.0.0")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--preview-limit", type=int, default=20)
    parser.add_argument(
        "--output",
        default="eval/deliverables/reports/v3_signal_extraction_dev.md",
        help="markdown output path",
    )
    parser.add_argument(
        "--json-output",
        default="eval/deliverables/reports/v3_signal_extraction_dev.json",
        help="json output path",
    )
    return parser.parse_args()


async def _main_async() -> None:
    args = _parse_args()
    script_ref = args.script_id.strip() or (_latest_script_id(default_engine) or "")
    if not script_ref:
        raise RuntimeError("no scripts found for signal smoke")
    script_id = resolve_script_id(script_ref, engine=default_engine) or script_ref

    caller = LlmCaller()
    await ensure_v1_tags_ready(script_id=script_id, caller=caller, engine=default_engine)

    rubric = load_rubric(args.rubric_version)
    ctx = build_signal_context(script_id=script_id, engine=default_engine)
    signal_values = await compute_signals(rubric, ctx, caller=caller, seed=args.seed)

    source_counts: dict[str, int] = {}
    coverage_by_dimension: dict[str, dict[str, float | int]] = {}
    for dim in rubric.dimensions:
        total = len(dim.signals)
        effective = 0
        for signal in dim.signals:
            value = signal_values.get(signal.id)
            if value and value.score is not None:
                effective += 1
        ratio = (effective / total) if total else 0.0
        coverage_by_dimension[dim.id] = {"effective": effective, "total": total, "ratio": ratio}

    serialized = {}
    for key in sorted(signal_values.keys()):
        obj = _serialize_signal(signal_values[key])
        serialized[key] = obj
        src = str(obj.get("source") or "unknown")
        source_counts[src] = source_counts.get(src, 0) + 1

    preview = [serialized[key] for key in list(serialized.keys())[: max(1, args.preview_limit)]]
    payload = {
        "script_id": script_id,
        "rubric_version": rubric.rubric_id,
        "seed": args.seed,
        "total_signals": len(serialized),
        "source_counts": source_counts,
        "coverage_by_dimension": coverage_by_dimension,
        "signals": serialized,
        "preview": preview,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_build_markdown(payload), encoding="utf-8")

    json_output = Path(args.json_output)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"output": str(output), "json_output": str(json_output), **payload}, ensure_ascii=False, indent=2))


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
