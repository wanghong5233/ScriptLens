from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import text

from service.tag_registry.loader import get_prompt_ver
from utils.database import engine as default_engine


ExtractorFn = Callable[[str, str, str, int, str], Awaitable[dict[str, Any]]]


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
class RunTrace:
    run_type: str  # intra|inter
    run_key: str   # seed:42 or variant:b
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
            row = [run.target_values.get(t, "") for t in targets]
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


async def _run_once(
    *,
    task: StabilityTask,
    target_id: str,
    extractor: ExtractorFn,
    seed: int,
    variant: str,
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
        payload = await extractor(target_id, task.tag_set_ver, prompt_ver, seed, variant)
        value = str(payload.get(task.dim, ""))
        output_hash = _hash_payload({"value": value})
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        await asyncio.to_thread(
            _persist_run,
            task=task,
            scope_id=target_id,
            prompt_ver=prompt_ver,
            model_ver=str(payload.get("__model_ver", "unknown")),
            seed=seed,
            input_hash=input_hash,
            output_hash=output_hash,
            status="success",
            error=None,
            metrics={"elapsed_ms": elapsed_ms, "variant": variant},
            started_at=started,
            finished_at=time.perf_counter(),
        )
        return value
    except Exception as e:
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
            error=f"{type(e).__name__}: {e}",
            metrics={"elapsed_ms": elapsed_ms, "variant": variant},
            started_at=started,
            finished_at=time.perf_counter(),
        )
        raise


async def run_intra_pss(task: StabilityTask, seeds: tuple[int, ...] = (42, 123, 456, 789, 1011)) -> RunResult:
    extractor = ExtractorRegistry.get(task.tag_set_ver, task.scope)
    result = RunResult(task=task)
    for seed in seeds:
        trace = RunTrace(run_type="intra", run_key=f"seed:{seed}")
        for target_id in task.targets:
            trace.target_values[target_id] = await _run_once(
                task=task,
                target_id=target_id,
                extractor=extractor,
                seed=seed,
                variant="a",
            )
        result.intra_runs.append(trace)
    return result


async def run_inter_pss(task: StabilityTask, variants: tuple[str, ...] = ("a", "b", "c"), seed: int = 42) -> RunResult:
    extractor = ExtractorRegistry.get(task.tag_set_ver, task.scope)
    result = RunResult(task=task)
    for variant in variants:
        trace = RunTrace(run_type="inter", run_key=f"variant:{variant}")
        for target_id in task.targets:
            trace.target_values[target_id] = await _run_once(
                task=task,
                target_id=target_id,
                extractor=extractor,
                seed=seed,
                variant=variant,
            )
        result.inter_runs.append(trace)
    return result


async def run_full(task: StabilityTask) -> RunResult:
    intra = await run_intra_pss(task)
    inter = await run_inter_pss(task)
    return RunResult(task=task, intra_runs=intra.intra_runs, inter_runs=inter.inter_runs)
