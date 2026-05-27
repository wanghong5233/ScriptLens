from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import text

from cli.ingest_dataset import ingest_dataset
from eval.generate_script_tag_stability_decision import generate_script_tag_stability_decision
from eval.stability.experiment_dir import ExperimentDir
from eval.stability.layer_a_runner import run_layer_a_repeat
from eval.stability.report import aggregate, write_markdown
from eval.stability.runner import ExtractorRegistry, StabilityTask, run_intra_pss
from service.script_tools.bundle_extractor import register_bundle_scope_extractors
from service.script_tools.character_entity_resolver import resolve_character_entities
from service.script_tools.plot_unit_segmenter import segment_plot_units
from service.script_tools.relationship_candidate_generator import ensure_relationship_candidates
from service.tag_registry.loader import list_bundles, load_tag_set
from utils.database import engine as default_engine

_SCRIPTLENS_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_SOURCE_DIR = _SCRIPTLENS_ROOT / "eval" / "ai漫剧剧本数据集" / "完整本"
_DEFAULT_INGEST_SUMMARY = Path(__file__).resolve().parents[1] / "eval" / "reports" / "dataset_ingest_summary.json"


def _version_prefix(tag_set_ver: str) -> str:
    return str(tag_set_ver).split(".")[0]


def _filter_version_bundles(tag_set_ver: str, bundles):
    prefix = _version_prefix(tag_set_ver)
    filtered = [bundle for bundle in bundles if str(getattr(bundle, "id", "")).startswith(prefix + "_")]
    return filtered or bundles


def _count_rows(table: str, script_id: str) -> int:
    with default_engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT COUNT(1) AS n FROM scriptlens.{table} WHERE script_id::text = :sid"),
            {"sid": script_id},
        ).mappings().first()
    return int(row["n"] if row else 0)


async def _bootstrap_structures(
    *,
    script_ids: list[str],
    tag_set_ver: str,
    scopes: list[str],
    seed: int,
) -> None:
    need_plot_units = any(scope in {"plot_unit", "relationship"} for scope in scopes)
    need_characters = any(scope in {"character", "relationship"} for scope in scopes)
    need_relationship = "relationship" in scopes

    if not any([need_plot_units, need_characters, need_relationship]):
        return

    for script_id in script_ids:
        if need_plot_units and _count_rows("plot_units", script_id) == 0:
            await segment_plot_units(script_id, tag_set_ver=tag_set_ver, seed=seed, variant="a", persist=True)
        if need_characters and _count_rows("character_entities", script_id) == 0:
            await resolve_character_entities(script_id, tag_set_ver=tag_set_ver, seed=seed, persist=True)
        if need_relationship and _count_rows("character_relationships", script_id) == 0:
            ensure_relationship_candidates(
                script_id,
                tag_set_ver=tag_set_ver,
                min_cooccurrence=1,
                top_k=30,
                persist=True,
                engine=default_engine,
            )


def _build_targets(scope: str, script_ids: list[str], tag_set_ver: str) -> list[str]:
    if scope == "script":
        return list(script_ids)
    if scope == "episode":
        out: list[str] = []
        for sid in script_ids:
            with default_engine.connect() as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT DISTINCT episode_no
                        FROM scriptlens.scenes
                        WHERE script_id::text = :sid AND episode_no IS NOT NULL
                        ORDER BY episode_no
                        LIMIT 120
                        """
                    ),
                    {"sid": sid},
                ).mappings().all()
            for row in rows:
                out.append(f"{sid}::ep::{int(row['episode_no'])}")
        return out
    if scope == "plot_unit":
        out: list[str] = []
        for sid in script_ids:
            with default_engine.connect() as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT id::text AS id
                        FROM scriptlens.plot_units
                        WHERE script_id::text = :sid
                        ORDER BY idx
                        LIMIT 400
                        """
                    ),
                    {"sid": sid},
                ).mappings().all()
            out.extend([str(row["id"]) for row in rows])
        return out
    if scope == "character":
        out: list[str] = []
        for sid in script_ids:
            with default_engine.connect() as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT id::text AS id
                        FROM scriptlens.character_entities
                        WHERE script_id::text = :sid
                        ORDER BY created_at, canonical_name
                        LIMIT 300
                        """
                    ),
                    {"sid": sid},
                ).mappings().all()
            out.extend([str(row["id"]) for row in rows])
        return out
    if scope == "relationship":
        out: list[str] = []
        for sid in script_ids:
            with default_engine.connect() as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT id::text AS id
                        FROM scriptlens.character_relationships
                        WHERE script_id::text = :sid
                          AND (tag_set_ver = :ver OR tag_set_ver = '' OR tag_set_ver IS NULL)
                        ORDER BY created_at, id
                        LIMIT 300
                        """
                    ),
                    {"sid": sid, "ver": tag_set_ver},
                ).mappings().all()
            out.extend([str(row["id"]) for row in rows])
        return out
    return list(script_ids)


async def _mock_extractor(
    target_id: str,
    tag_set_ver: str,
    prompt_ver: str,
    seed: int,
    variant: str,
    use_cache: bool = True,  # noqa: ARG001
) -> dict:
    cfg = load_tag_set(tag_set_ver)
    parts = prompt_ver.split(":")
    dim = parts[1] if len(parts) >= 2 else ""
    dim_cfg = cfg.get_dim(dim)
    values = list(dim_cfg.values)
    if not values:
        return {dim: "none", "__model_ver": "mock-model"}
    digest = hashlib.sha256(f"{target_id}|{seed}|{variant}|{dim}".encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(values)
    return {dim: values[idx], "__model_ver": "mock-model"}


def _new_run_id(model_name: str) -> str:
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    model_slug = (model_name or "unknown").replace("-", "").replace(".", "")
    return f"{timestamp}_{model_slug}"


@contextmanager
def _cache_env(disable_cache: bool) -> Iterator[None]:
    prev_value = os.getenv("SM_STABILITY_DISABLE_CACHE")
    if disable_cache:
        os.environ["SM_STABILITY_DISABLE_CACHE"] = "1"
    try:
        yield
    finally:
        if disable_cache:
            if prev_value is None:
                os.environ.pop("SM_STABILITY_DISABLE_CACHE", None)
            else:
                os.environ["SM_STABILITY_DISABLE_CACHE"] = prev_value


def _resolve_scopes(args: argparse.Namespace, tag_set_ver: str) -> list[str]:
    if args.scope:
        return [args.scope]
    bundles = _filter_version_bundles(tag_set_ver, list_bundles(tag_set_ver))
    if bundles:
        return sorted({bundle.scope for bundle in bundles})
    cfg = load_tag_set(tag_set_ver)
    return list(cfg.scope_to_dims.keys())


def _resolve_dims_filter(args: argparse.Namespace, scopes: list[str], tag_set_ver: str) -> set[str]:
    cfg = load_tag_set(tag_set_ver)
    dims_filter = {dim.strip() for dim in args.dims.split(",") if dim.strip()}
    if dims_filter:
        return dims_filter
    for scope in scopes:
        scoped_bundles = list_bundles(tag_set_ver, scope=scope)
        for bundle in _filter_version_bundles(tag_set_ver, scoped_bundles):
            for dim in bundle.dims:
                dims_filter.add(dim)
        if scope in cfg.scope_to_dims:
            for dim_cfg in cfg.scope_to_dims.get(scope, ()):
                dims_filter.add(dim_cfg.dim)
    return dims_filter


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run script-side tag stability experiment (Batch5.5).",
        epilog=(
            "Env sanity: SM_LLM_TYPE=dashscope / "
            "DASHSCOPE_MODEL_NAME=qwen3-max / "
            "SM_LLM_MODEL_AUX=qwen3-max"
        ),
    )
    parser.add_argument("--tag-set", required=True, dest="tag_set")
    parser.add_argument("--source-dir", default=str(_DEFAULT_SOURCE_DIR), help="script dataset directory")
    parser.add_argument("--n-scripts", type=int, default=0, help="limit scripts. 0 means all")
    parser.add_argument("--n-repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--run-id", default="", help="existing run id for resume, or explicit new run id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dims", default="", help="comma-separated dims; empty means all dims under scope(s)")
    parser.add_argument("--scope", default="", help="script|episode|plot_unit|character|relationship")
    parser.add_argument("--user-id", type=int, default=1, help="script owner id used by dataset ingest")
    parser.add_argument(
        "--disable-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="disable llm_cache + in-memory payload cache during experiment",
    )
    parser.add_argument(
        "--include-layer-a",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="run layer-a structural stability",
    )
    parser.add_argument("--retry-budget", type=int, default=1, help="reserved retry budget for failed targets")
    return parser.parse_args()


async def _main_async() -> None:
    args = _parse_args()
    cfg = load_tag_set(args.tag_set)

    try:
        register_bundle_scope_extractors(args.tag_set)
    except Exception:
        pass

    scopes = _resolve_scopes(args, args.tag_set)
    dims_filter = _resolve_dims_filter(args, scopes, args.tag_set)

    if args.resume and not args.run_id:
        raise ValueError("--resume requires --run-id")

    ingest_summary: dict = {}
    if args.resume:
        exp_dir = ExperimentDir.load(args.run_id)
        script_ids = [str(script_id) for script_id in exp_dir.manifest.get("scripts", [])]
        if not script_ids:
            raise RuntimeError(f"manifest has empty scripts: {exp_dir.manifest_path}")
    else:
        ingest_summary = ingest_dataset(
            dataset_dir=Path(args.source_dir),
            user_id=args.user_id,
            skip_unsupported=True,
            limit=None,
            summary_output=_DEFAULT_INGEST_SUMMARY,
        )
        script_ids = [str(row["script_id"]) for row in ingest_summary.get("ok", [])]
        if args.n_scripts > 0:
            script_ids = script_ids[: args.n_scripts]
        if not script_ids:
            raise RuntimeError(f"no scripts ingested from {args.source_dir}")

        provider = os.getenv("SM_LLM_TYPE", "dashscope")
        model = os.getenv("DASHSCOPE_MODEL_NAME", "qwen3-max")
        explicit_run_id = args.run_id.strip() or _new_run_id(model)
        run_dir = ExperimentDir.default_root() / explicit_run_id
        if run_dir.exists():
            raise FileExistsError(f"run_id already exists: {explicit_run_id}; use --resume to continue")
        exp_dir = ExperimentDir.create(
            tag_set_ver=args.tag_set,
            provider=provider,
            model=model,
            seed=args.seed,
            temperature=0.0,
            n_repeats=args.n_repeats,
            scripts=script_ids,
            cache_disabled=bool(args.disable_cache),
            run_id=explicit_run_id,
        )
        exp_dir.update_stage("ingest", "done")
        failed_rows = ingest_summary.get("failed") if isinstance(ingest_summary, dict) else None
        if isinstance(failed_rows, list) and failed_rows:
            failed_path = exp_dir.run_dir / "failed.json"
            failed_path.write_text(json.dumps({"failed": failed_rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    for scope in scopes:
        if not ExtractorRegistry.has(args.tag_set, scope):
            ExtractorRegistry.register(args.tag_set, scope, _mock_extractor)

    with _cache_env(bool(args.disable_cache)):
        if exp_dir.manifest.get("stages", {}).get("freeze") != "done":
            await _bootstrap_structures(
                script_ids=script_ids,
                tag_set_ver=args.tag_set,
                scopes=scopes,
                seed=args.seed,
            )
            exp_dir.update_stage("freeze", "done")

        layer_a_reports = []
        if bool(args.include_layer_a):
            exp_dir.update_stage("layer_a", "in_progress")
            for script_id in script_ids:
                report = await run_layer_a_repeat(
                    script_id,
                    seed=args.seed,
                    n_repeats=args.n_repeats,
                    tag_set_ver=args.tag_set,
                    exp_dir=exp_dir,
                    use_cache=not args.disable_cache,
                    engine=default_engine,
                )
                layer_a_reports.append(report)
            exp_dir.save_aggregated_layer_a(layer_a_reports)
            exp_dir.update_stage("layer_a", "done")

        exp_dir.update_stage("layer_b", "in_progress")
        run_results: dict[str, Any] = {}
        for scope in scopes:
            targets = _build_targets(scope, script_ids, args.tag_set)
            if not targets:
                continue
            for dim_cfg in cfg.scope_to_dims.get(scope, ()):
                if dim_cfg.dim not in dims_filter:
                    continue
                task = StabilityTask(
                    tag_set_ver=args.tag_set,
                    dim=dim_cfg.dim,
                    scope=scope,
                    targets=targets,
                )
                run_results[dim_cfg.dim] = await run_intra_pss(
                    task,
                    seed=args.seed,
                    n_repeats=args.n_repeats,
                    exp_dir=exp_dir,
                    use_cache=not args.disable_cache,
                )
        reports = aggregate(run_results)
        exp_dir.save_aggregated_layer_b(reports)
        write_markdown(
            reports,
            str(exp_dir.run_dir / "aggregated" / "layer_b.md"),
            tag_set_ver=args.tag_set,
            split="full",
        )
        exp_dir.update_stage("layer_b", "done")

    decision_paths = generate_script_tag_stability_decision(run_dir=exp_dir.run_dir)
    print(
        json.dumps(
            {
                "run_id": exp_dir.run_id,
                "run_dir": str(exp_dir.run_dir),
                "script_count": len(script_ids),
                "dims_count": len(dims_filter),
                "decision_md": decision_paths["run_decision_md"],
                "project_decision_md": decision_paths["project_decision_md"],
                "ingest_summary": ingest_summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
