from __future__ import annotations

import argparse
import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

from eval.stability.report import aggregate, write_markdown, write_per_dim_json
from eval.stability.runner import ExtractorRegistry, StabilityTask, run_full
from eval.stability.sampler import sample_split
from service.script_tools.bundle_extractor import register_bundle_scope_extractors
from service.script_tools.character_entity_resolver import resolve_character_entities
from service.script_tools.plot_unit_segmenter import segment_plot_units
from service.script_tools.relationship_candidate_generator import ensure_relationship_candidates
from service.script_tools.v0_extractor_common import resolve_script_id
from service.tag_registry.loader import list_bundles, load_tag_set
from utils.database import engine as default_engine


@dataclass
class _SampleFallback:
    script_id: str
    episodes_sampled: list[int]


def _resolve_script_ids(script_refs: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for ref in script_refs:
        sid = resolve_script_id(ref, engine=default_engine) or ref
        if sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def _samples_from_db(limit: int = 10, n_episodes: int = 5) -> list[_SampleFallback]:
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
    samples: list[_SampleFallback] = []
    for row in rows:
        samples.append(
            _SampleFallback(
                script_id=str(row["id"]),
                episodes_sampled=list(range(1, n_episodes + 1)),
            )
        )
    return samples


def _version_prefix(tag_set_ver: str) -> str:
    return str(tag_set_ver).split(".")[0]


def _filter_version_bundles(tag_set_ver: str, bundles):
    prefix = _version_prefix(tag_set_ver)
    filtered = [b for b in bundles if str(getattr(b, "id", "")).startswith(prefix + "_")]
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
) -> None:
    need_plot_units = any(scope in {"plot_unit", "relationship"} for scope in scopes)
    need_characters = any(scope in {"character", "relationship"} for scope in scopes)
    need_relationship = "relationship" in scopes

    if not any([need_plot_units, need_characters, need_relationship]):
        return

    for script_id in script_ids:
        if need_plot_units and _count_rows("plot_units", script_id) == 0:
            try:
                await segment_plot_units(script_id, tag_set_ver=tag_set_ver, seed=42, variant="a", persist=True)
            except Exception:
                pass
        if need_characters and _count_rows("character_entities", script_id) == 0:
            try:
                await resolve_character_entities(script_id, tag_set_ver=tag_set_ver, seed=42, persist=True)
            except Exception:
                pass
        if need_relationship:
            try:
                ensure_relationship_candidates(
                    script_id,
                    tag_set_ver=tag_set_ver,
                    min_cooccurrence=1,
                    top_k=20,
                    persist=True,
                    engine=default_engine,
                )
            except Exception:
                pass


def _build_targets(scope: str, script_ids: list[str], episodes_by_script: dict[str, list[int]], tag_set_ver: str) -> list[str]:
    if scope == "script":
        return script_ids
    if scope == "episode":
        out: list[str] = []
        for sid in script_ids:
            episode_nos: list[int] = []
            with default_engine.connect() as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT DISTINCT episode_no
                        FROM scriptlens.scenes
                        WHERE script_id::text = :sid AND episode_no IS NOT NULL
                        ORDER BY episode_no
                        LIMIT 50
                        """
                    ),
                    {"sid": sid},
                ).mappings().all()
            episode_nos = [int(r["episode_no"]) for r in rows if r.get("episode_no") is not None]
            if not episode_nos:
                episode_nos = episodes_by_script.get(sid, [])
            for ep in episode_nos:
                out.append(f"{sid}::ep::{ep}")
        return out
    if scope == "plot_unit":
        out = []
        for sid in script_ids:
            with default_engine.connect() as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT id::text AS id
                        FROM scriptlens.plot_units
                        WHERE script_id::text = :sid
                        ORDER BY idx
                LIMIT 30
                        """
                    ),
                    {"sid": sid},
                ).mappings().all()
            if rows:
                out.extend([str(r["id"]) for r in rows])
            else:
                for idx in range(1, 7):
                    out.append(f"{sid}::plot::{idx}")
        return out
    if scope == "character":
        out = []
        for sid in script_ids:
            with default_engine.connect() as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT id::text AS id
                        FROM scriptlens.character_entities
                        WHERE script_id::text = :sid
                        ORDER BY created_at, canonical_name
                LIMIT 30
                        """
                    ),
                    {"sid": sid},
                ).mappings().all()
            if rows:
                out.extend([str(r["id"]) for r in rows])
            else:
                for idx in range(1, 5):
                    out.append(f"{sid}::char::{idx}")
        return out
    if scope == "relationship":
        out = []
        for sid in script_ids:
            with default_engine.connect() as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT id::text AS id
                        FROM scriptlens.character_relationships
                        WHERE script_id::text = :sid AND (tag_set_ver = :ver OR tag_set_ver = '')
                        ORDER BY created_at, id
                LIMIT 30
                        """
                    ),
                    {"sid": sid, "ver": tag_set_ver},
                ).mappings().all()
            if rows:
                out.extend([str(r["id"]) for r in rows])
            else:
                for idx in range(1, 5):
                    out.append(f"{sid}::rel::{idx}")
        return out
    return script_ids


async def _mock_extractor(
    target_id: str,
    tag_set_ver: str,
    prompt_ver: str,
    seed: int,
    variant: str,
) -> dict:
    cfg = load_tag_set(tag_set_ver)
    # prompt_ver format: "{prompt_ver}:{dim}:{variant}"
    parts = prompt_ver.split(":")
    dim = parts[1] if len(parts) >= 2 else ""
    dim_cfg = cfg.get_dim(dim)
    values = list(dim_cfg.values)
    if not values:
        return {dim: "none", "__model_ver": "mock-model"}
    digest = hashlib.sha256(f"{target_id}|{seed}|{variant}|{dim}".encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(values)
    return {dim: values[idx], "__model_ver": "mock-model"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run stability evaluation for tag dimensions.")
    parser.add_argument("--tag-set", required=True, dest="tag_set")
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    parser.add_argument("--dims", default="", help="Comma-separated dims. Empty means all dims in selected scope(s).")
    parser.add_argument("--scope", default="", help="script|episode|plot_unit|character|relationship")
    parser.add_argument("--output", default="", help="Markdown report output path")
    parser.add_argument(
        "--source-dir",
        default="ScriptLens/eval/爆款短剧剧本（完整本）",
        help="Script dataset directory",
    )
    return parser.parse_args()


async def _main_async() -> None:
    args = _parse_args()
    cfg = load_tag_set(args.tag_set)

    try:
        register_bundle_scope_extractors(args.tag_set)
    except Exception:
        # Keep CLI usable even when runtime dependencies are missing.
        pass

    samples = sample_split(args.split, source_dir=args.source_dir)
    if not samples:
        samples = _samples_from_db(limit=10, n_episodes=5)
    if not samples:
        raise RuntimeError(f"no samples found under {args.source_dir}, and DB fallback is empty")

    raw_script_refs = [s.script_id for s in samples]
    script_ids = _resolve_script_ids(raw_script_refs)
    episodes_by_script = {resolve_script_id(s.script_id, engine=default_engine) or s.script_id: s.episodes_sampled for s in samples}

    if args.scope:
        scopes = [args.scope]
    else:
        bundles = _filter_version_bundles(args.tag_set, list_bundles(args.tag_set))
        if bundles:
            scopes = sorted({b.scope for b in bundles})
        else:
            scopes = list(cfg.scope_to_dims.keys())
    dims_filter = {d.strip() for d in args.dims.split(",") if d.strip()}

    if not dims_filter:
        for scope in scopes:
            scoped_bundles = list_bundles(args.tag_set, scope=scope)
            if scoped_bundles:
                for bundle in _filter_version_bundles(args.tag_set, scoped_bundles):
                    for dim in bundle.dims:
                        dims_filter.add(dim)
            else:
                for dim_cfg in cfg.scope_to_dims.get(scope, ()):
                    dims_filter.add(dim_cfg.dim)

    await _bootstrap_structures(script_ids=script_ids, tag_set_ver=args.tag_set, scopes=scopes)

    # If no real extractor is registered yet, use deterministic mock extractor.
    for scope in scopes:
        if not ExtractorRegistry.has(args.tag_set, scope):
            ExtractorRegistry.register(args.tag_set, scope, _mock_extractor)

    run_results = {}
    for scope in scopes:
        targets = _build_targets(scope, script_ids, episodes_by_script, args.tag_set)
        for dim_cfg in cfg.scope_to_dims.get(scope, ()):
            if dim_cfg.dim not in dims_filter:
                continue
            task = StabilityTask(
                tag_set_ver=args.tag_set,
                dim=dim_cfg.dim,
                scope=scope,
                targets=targets,
            )
            run_results[dim_cfg.dim] = await run_full(task)

    reports = aggregate(run_results)

    output = args.output
    if not output:
        output = str(Path("ScriptLens/eval/reports") / f"{args.tag_set}_{args.split}.md")
    write_markdown(reports, output, tag_set_ver=args.tag_set, split=args.split)
    write_per_dim_json(reports, str(Path(output).with_suffix("")) + "_per_dim")
    print(f"stability report generated: {output}")


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
