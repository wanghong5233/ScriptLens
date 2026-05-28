from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import text

from eval.stability.experiment_dir import ExperimentDir
from service.tag_registry.loader import get_prompt_ver, load_bundle
from utils.database import engine as default_engine

ExtractorFn = Callable[..., Awaitable[dict[str, Any]]]


class ExtractorRegistry:
    _registry: dict[tuple[str, str], ExtractorFn] = {}

    @classmethod
    def register(cls, tag_set_ver: str, scope: str, fn: ExtractorFn) -> None:
        cls._registry[(tag_set_ver, scope)] = fn

    @classmethod
    def get(cls, tag_set_ver: str, scope: str) -> ExtractorFn:
        key = (tag_set_ver, scope)
        if key not in cls._registry:
            raise KeyError(f"extractor not registered for tag_set={tag_set_ver}, scope={scope}")
        return cls._registry[key]

    @classmethod
    def has(cls, tag_set_ver: str, scope: str) -> bool:
        return (tag_set_ver, scope) in cls._registry


@dataclass
class StabilityTask:
    tag_set_ver: str
    dim: str
    scope: str
    targets: list[str]


@dataclass
class BundleStabilityTask:
    tag_set_ver: str
    bundle_id: str
    scope: str
    dims: list[str]
    targets: list[str]


@dataclass
class RunTrace:
    run_type: str  # intra|inter
    run_key: str   # rep:0 or variant:b
    target_values: dict[str, str] = field(default_factory=dict)


@dataclass
class RunResult:
    task: StabilityTask
    intra_runs: list[RunTrace] = field(default_factory=list)
    inter_runs: list[RunTrace] = field(default_factory=list)

    def target_ids(self) -> list[str]:
        ids = set()
        for run in [*self.intra_runs, *self.inter_runs]:
            ids.update(run.target_values.keys())
        return sorted(ids)

    def matrix(self, run_type: str) -> list[list[str]]:
        runs = self.intra_runs if run_type == "intra" else self.inter_runs
        targets = self.target_ids()
        matrix: list[list[str]] = []
        for run in runs:
            row = [run.target_values.get(target_id, "") for target_id in targets]
            matrix.append(row)
        return matrix


def _hash_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _persist_run(
    *,
    task: StabilityTask,
    scope_id: str,
    prompt_ver: str,
    model_ver: str,
    seed: int,
    input_hash: str,
    output_hash: str | None,
    status: str,
    error: str | None,
    metrics: dict[str, Any],
    started_at: float,
    finished_at: float | None,
) -> None:
    rid = str(uuid.uuid4())
    finished_sql = "NOW()" if finished_at is not None else "NULL"
    with default_engine.begin() as conn:
        conn.execute(
            text(
                f"""
                INSERT INTO scriptlens.tag_extraction_runs
                    (id, script_id, scope, scope_id, tag_set_ver, prompt_ver, model_ver, seed,
                     input_hash, output_hash, status, error, metrics, started_at, finished_at)
                VALUES
                    (:id, NULL, :scope, :scope_id, :tag_set_ver, :prompt_ver, :model_ver, :seed,
                     :input_hash, :output_hash, :status, :error, CAST(:metrics AS jsonb), NOW(), {finished_sql})
                """
            ),
            {
                "id": rid,
                "scope": task.scope,
                "scope_id": scope_id,
                "tag_set_ver": task.tag_set_ver,
                "prompt_ver": prompt_ver,
                "model_ver": model_ver,
                "seed": seed,
                "input_hash": input_hash,
                "output_hash": output_hash,
                "status": status,
                "error": error,
                "metrics": json.dumps(metrics, ensure_ascii=False),
            },
        )


def _target_script_id(scope: str, target_id: str) -> str:
    if scope == "script":
        return target_id
    if scope == "episode" and "::ep::" in target_id:
        return target_id.partition("::ep::")[0]
    table_by_scope = {
        "plot_unit": "plot_units",
        "character": "character_entities",
        "relationship": "character_relationships",
    }
    table = table_by_scope.get(scope)
    if table is None:
        return target_id
    with default_engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT script_id::text AS script_id FROM scriptlens.{table} WHERE id::text = :tid LIMIT 1"),
            {"tid": target_id},
        ).mappings().first()
    if row and row.get("script_id"):
        return str(row["script_id"])
    return target_id


async def _run_once(
    *,
    task: StabilityTask,
    target_id: str,
    extractor: ExtractorFn,
    seed: int,
    variant: str,
    rep: int,
    script_id: str,
    use_cache: bool,
    exp_dir: ExperimentDir | None,
) -> str:
    prompt_ver = get_prompt_ver(task.tag_set_ver, task.dim, variant=variant)
    input_hash = _hash_payload(
        {
            "target_id": target_id,
            "tag_set_ver": task.tag_set_ver,
            "dim": task.dim,
            "seed": seed,
            "variant": variant,
            "prompt_ver": prompt_ver,
        }
    )
    started = time.perf_counter()
    try:
        try:
            payload = await extractor(target_id, task.tag_set_ver, prompt_ver, seed, variant, use_cache=use_cache)
        except TypeError:
            payload = await extractor(target_id, task.tag_set_ver, prompt_ver, seed, variant)
        value = str(payload.get(task.dim, ""))
        output_hash = _hash_payload({"value": value})
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        model_ver = str(payload.get("__model_ver", "unknown"))
        await asyncio.to_thread(
            _persist_run,
            task=task,
            scope_id=target_id,
            prompt_ver=prompt_ver,
            model_ver=model_ver,
            seed=seed,
            input_hash=input_hash,
            output_hash=output_hash,
            status="success",
            error=None,
            metrics={"elapsed_ms": elapsed_ms, "variant": variant, "rep": rep},
            started_at=started,
            finished_at=time.perf_counter(),
        )
        if exp_dir is not None:
            exp_dir.append_tag_value_raw(
                script_id,
                task.scope,
                task.dim,
                rep,
                target_id,
                value,
                {
                    "model_ver": model_ver,
                    "elapsed_ms": elapsed_ms,
                    "seed": seed,
                    "prompt_ver": prompt_ver,
                },
            )
        return value
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        await asyncio.to_thread(
            _persist_run,
            task=task,
            scope_id=target_id,
            prompt_ver=prompt_ver,
            model_ver="unknown",
            seed=seed,
            input_hash=input_hash,
            output_hash=None,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            metrics={"elapsed_ms": elapsed_ms, "variant": variant, "rep": rep},
            started_at=started,
            finished_at=time.perf_counter(),
        )
        raise


async def run_intra_pss(
    task: StabilityTask,
    *,
    seed: int = 42,
    n_repeats: int = 5,
    exp_dir: ExperimentDir | None = None,
    use_cache: bool = False,
) -> RunResult:
    extractor = ExtractorRegistry.get(task.tag_set_ver, task.scope)
    result = RunResult(task=task)

    script_to_targets: dict[str, list[str]] = {}
    for target_id in task.targets:
        script_id = _target_script_id(task.scope, target_id)
        script_to_targets.setdefault(script_id, []).append(target_id)

    for rep in range(n_repeats):
        trace = RunTrace(run_type="intra", run_key=f"rep:{rep}")
        for script_id, target_ids in script_to_targets.items():
            existing = exp_dir.tag_value_map(script_id, task.scope, task.dim, rep) if exp_dir is not None else {}
            rep_done = (
                exp_dir.is_tag_value_done(
                    script_id,
                    task.scope,
                    task.dim,
                    rep,
                    target_count=len(target_ids),
                )
                if exp_dir is not None
                else False
            )
            if rep_done:
                for target_id in target_ids:
                    trace.target_values[target_id] = str(existing.get(target_id, ""))
                continue

            for target_id in target_ids:
                if target_id in existing:
                    trace.target_values[target_id] = str(existing[target_id])
                    continue
                trace.target_values[target_id] = await _run_once(
                    task=task,
                    target_id=target_id,
                    extractor=extractor,
                    seed=seed,
                    variant="a",
                    rep=rep,
                    script_id=script_id,
                    use_cache=use_cache,
                    exp_dir=exp_dir,
                )
        result.intra_runs.append(trace)
    return result


async def run_inter_pss(
    task: StabilityTask,
    variants: tuple[str, ...] = ("a", "b", "c"),
    seed: int = 42,
    use_cache: bool = False,
) -> RunResult:
    extractor = ExtractorRegistry.get(task.tag_set_ver, task.scope)
    result = RunResult(task=task)
    for variant in variants:
        trace = RunTrace(run_type="inter", run_key=f"variant:{variant}")
        for target_id in task.targets:
            script_id = _target_script_id(task.scope, target_id)
            trace.target_values[target_id] = await _run_once(
                task=task,
                target_id=target_id,
                extractor=extractor,
                seed=seed,
                variant=variant,
                rep=0,
                script_id=script_id,
                use_cache=use_cache,
                exp_dir=None,
            )
        result.inter_runs.append(trace)
    return result


async def run_full(task: StabilityTask) -> RunResult:
    intra = await run_intra_pss(task, seed=42, n_repeats=5, exp_dir=None, use_cache=False)
    inter = await run_inter_pss(task, seed=42, use_cache=False)
    return RunResult(task=task, intra_runs=intra.intra_runs, inter_runs=inter.inter_runs)


async def run_bundle_stability(
    task: BundleStabilityTask,
    *,
    seed: int = 42,
    n_repeats: int = 5,
    exp_dir: ExperimentDir | None = None,
    use_cache: bool = False,
) -> dict[str, RunResult]:
    """Run one bundle stability with single-call extraction per target/rep.

    Returns per-dim RunResult so downstream aggregate() can stay dim-oriented.
    """
    if not task.dims:
        return {}
    extractor = ExtractorRegistry.get(task.tag_set_ver, task.scope)
    bundle = load_bundle(task.tag_set_ver, task.bundle_id)
    if not bundle.dims:
        return {}
    # extract_by_scope resolves bundle by dim from prompt_ver;
    # we pass the first dim's prompt_ver so one call returns all bundle dims.
    bundle_dim_for_prompt = bundle.dims[0]

    results: dict[str, RunResult] = {
        dim: RunResult(task=StabilityTask(task.tag_set_ver, dim, task.scope, list(task.targets)))
        for dim in task.dims
    }

    script_to_targets: dict[str, list[str]] = {}
    for target_id in task.targets:
        script_id = _target_script_id(task.scope, target_id)
        script_to_targets.setdefault(script_id, []).append(target_id)

    for rep in range(n_repeats):
        traces_by_dim = {
            dim: RunTrace(run_type="intra", run_key=f"rep:{rep}", target_values={})
            for dim in task.dims
        }
        for script_id, target_ids in script_to_targets.items():
            existing_by_dim = {
                dim: (exp_dir.tag_value_map(script_id, task.scope, dim, rep) if exp_dir is not None else {})
                for dim in task.dims
            }
            for target_id in target_ids:
                if all(target_id in existing_by_dim[dim] for dim in task.dims):
                    for dim in task.dims:
                        traces_by_dim[dim].target_values[target_id] = str(existing_by_dim[dim][target_id])
                    continue

                prompt_ver = get_prompt_ver(task.tag_set_ver, bundle_dim_for_prompt, variant="a")
                started = time.perf_counter()
                input_hash = _hash_payload(
                    {
                        "target_id": target_id,
                        "tag_set_ver": task.tag_set_ver,
                        "bundle_id": task.bundle_id,
                        "seed": seed,
                        "variant": "a",
                        "prompt_ver": prompt_ver,
                    }
                )
                try:
                    try:
                        payload = await extractor(
                            target_id, task.tag_set_ver, prompt_ver, seed, "a", use_cache=use_cache
                        )
                    except TypeError:
                        payload = await extractor(target_id, task.tag_set_ver, prompt_ver, seed, "a")
                except Exception as exc:
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    for dim in task.dims:
                        dim_prompt_ver = get_prompt_ver(task.tag_set_ver, dim, variant="a")
                        await asyncio.to_thread(
                            _persist_run,
                            task=StabilityTask(task.tag_set_ver, dim, task.scope, []),
                            scope_id=target_id,
                            prompt_ver=dim_prompt_ver,
                            model_ver="unknown",
                            seed=seed,
                            input_hash=input_hash,
                            output_hash=None,
                            status="failed",
                            error=f"{type(exc).__name__}: {exc}",
                            metrics={"elapsed_ms": elapsed_ms, "variant": "a", "rep": rep, "bundle_id": task.bundle_id},
                            started_at=started,
                            finished_at=time.perf_counter(),
                        )
                    raise

                elapsed_ms = int((time.perf_counter() - started) * 1000)
                model_ver = str(payload.get("__model_ver", "unknown"))
                for dim in task.dims:
                    value = str(payload.get(dim, ""))
                    traces_by_dim[dim].target_values[target_id] = value
                    dim_prompt_ver = get_prompt_ver(task.tag_set_ver, dim, variant="a")
                    output_hash = _hash_payload({"value": value})
                    await asyncio.to_thread(
                        _persist_run,
                        task=StabilityTask(task.tag_set_ver, dim, task.scope, []),
                        scope_id=target_id,
                        prompt_ver=dim_prompt_ver,
                        model_ver=model_ver,
                        seed=seed,
                        input_hash=input_hash,
                        output_hash=output_hash,
                        status="success",
                        error=None,
                        metrics={"elapsed_ms": elapsed_ms, "variant": "a", "rep": rep, "bundle_id": task.bundle_id},
                        started_at=started,
                        finished_at=time.perf_counter(),
                    )
                    if exp_dir is not None:
                        exp_dir.append_tag_value_raw(
                            script_id,
                            task.scope,
                            dim,
                            rep,
                            target_id,
                            value,
                            {
                                "model_ver": model_ver,
                                "elapsed_ms": elapsed_ms,
                                "seed": seed,
                                "prompt_ver": dim_prompt_ver,
                                "bundle_id": task.bundle_id,
                            },
                        )

        for dim, trace in traces_by_dim.items():
            results[dim].intra_runs.append(trace)

    return results
