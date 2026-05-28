from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import text

from cli.ingest_dataset import ingest_dataset
from eval.generate_script_tag_stability_decision import generate_script_tag_stability_decision
from eval.stability.concurrency import AIMD, aimd_call, aimd_ramp_watcher
from eval.stability.experiment_dir import ExperimentDir
from eval.stability.report import aggregate, write_markdown
from eval.stability.runner import (
    BundleStabilityTask,
    ExtractorRegistry,
    RunResult,
    RunTrace,
    StabilityTask,
    _hash_payload,
    _persist_run,
    _target_script_id,
)
from eval.stability.segmentation_runner import (
    aggregate_segmentation,
    run_one_segmentation_step,
)
from service.script_tools.bundle_extractor import register_bundle_scope_extractors
from service.script_tools.character_entity_resolver import resolve_character_entities
from service.script_tools.plot_unit_segmenter import segment_plot_units
from service.script_tools.relationship_candidate_generator import ensure_relationship_candidates
from service.tag_registry.loader import get_prompt_ver, list_bundles, load_tag_set
from utils.database import engine as default_engine

_DATASET_RELATIVE = Path("eval") / "ai漫剧剧本数据集" / "完整本"


def _resolve_scriptlens_root() -> Path:
    """定位 ScriptLens 仓库根目录，宿主机 / 容器双兼容。"""
    env_root = os.getenv("SCRIPTLENS_ROOT")
    if env_root:
        return Path(env_root)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / _DATASET_RELATIVE).is_dir():
            return parent
    if (Path("/") / _DATASET_RELATIVE).is_dir():
        return Path("/")
    parents = list(here.parents)
    return parents[-1] if parents else here


_SCRIPTLENS_ROOT = _resolve_scriptlens_root()
_DEFAULT_SOURCE_DIR = _SCRIPTLENS_ROOT / _DATASET_RELATIVE
_DEFAULT_INGEST_SUMMARY = Path(__file__).resolve().parents[1] / "eval" / "reports" / "dataset_ingest_summary.json"


def _preflight(args: argparse.Namespace) -> None:
    """实验前置契约校验：environment / dataset 任何一项不满足，立刻 fail-loud。"""
    errors: list[str] = []

    if not os.getenv("DASHSCOPE_API_KEY"):
        errors.append(
            "DASHSCOPE_API_KEY 未设置。run_stability 依赖千问 LLM；"
            "容器内执行时 .env.dev 已注入 key，请确认你是用 `docker exec scriptlens_api_dev ...` 调用的。"
        )

    # 实验期间禁止配 OPENAI_API_KEY：LlmCaller 把 RateLimitError 视为可切 provider 的兜底信号，
    # 一旦 DashScope 限流就会自动切到 OpenAI，混 provider 会污染稳定性测量。
    if os.getenv("OPENAI_API_KEY"):
        errors.append(
            "OPENAI_API_KEY 已设置。稳定性实验必须只用单一 provider，否则 LlmCaller 在 DashScope 限流时"
            "会自动切到 OpenAI 兜底，污染测量。请在容器 env 中清掉 OPENAI_API_KEY 或临时 `unset` 后再跑。"
        )

    source_dir = Path(args.source_dir)
    if not args.resume:
        if not source_dir.exists():
            errors.append(
                f"--source-dir 不存在: {source_dir}\n"
                "  容器内默认值 /eval/ai漫剧剧本数据集/完整本 依赖 docker-compose.dev.yml 把 "
                "../eval 挂到 /eval:ro。请确认 compose volumes 已更新并重启 scriptlens_api_dev。"
            )
        elif not any(source_dir.iterdir()):
            errors.append(f"--source-dir 为空: {source_dir}")

    try:
        load_tag_set(args.tag_set)
    except Exception as exc:
        errors.append(f"--tag-set {args.tag_set!r} 加载失败: {type(exc).__name__}: {exc}")

    if args.sample_size <= 0:
        errors.append(f"--sample-size 必须为正整数，得到 {args.sample_size}")

    if args.concurrency <= 0:
        errors.append(f"--concurrency 必须为正整数，得到 {args.concurrency}")

    if errors:
        msg = "\n".join(f"  - {e}" for e in errors)
        raise SystemExit(f"[run_stability] preflight failed:\n{msg}")


def _all_bundles(tag_set_ver: str, *, scope: str | None = None):
    """当前 tag_set 下所有 bundles 都参与稳定性测试。"""
    return list_bundles(tag_set_ver, scope=scope)


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
    aimd: AIMD | None = None,
) -> None:
    """第一层结束后固定输入池：为后续标签实验准备 plot_units / characters / relationships。

    多剧本并发：用 asyncio.gather + aimd 限流；不同 script_id 主键互不重叠，PG 写入安全。
    """
    need_plot_units = any(scope in {"plot_unit", "relationship"} for scope in scopes)
    need_characters = any(scope in {"character", "relationship"} for scope in scopes)
    need_relationship = "relationship" in scopes

    if not any([need_plot_units, need_characters, need_relationship]):
        return

    total = len(script_ids)
    counter = {"done": 0}
    counter_lock = asyncio.Lock()

    async def _freeze_one(script_id: str) -> None:
        sub_t0 = time.time()
        if need_plot_units and _count_rows("plot_units", script_id) == 0:
            if aimd is not None:
                await aimd_call(
                    aimd,
                    lambda: segment_plot_units(
                        script_id, tag_set_ver=tag_set_ver, seed=seed, variant="a", persist=True
                    ),
                )
            else:
                await segment_plot_units(script_id, tag_set_ver=tag_set_ver, seed=seed, variant="a", persist=True)
        if need_characters and _count_rows("character_entities", script_id) == 0:
            if aimd is not None:
                await aimd_call(
                    aimd,
                    lambda: resolve_character_entities(
                        script_id, tag_set_ver=tag_set_ver, seed=seed, persist=True
                    ),
                )
            else:
                await resolve_character_entities(script_id, tag_set_ver=tag_set_ver, seed=seed, persist=True)
        if need_relationship and _count_rows("character_relationships", script_id) == 0:
            # ensure_relationship_candidates 是同步函数，丢线程池跑避免阻塞事件循环
            await asyncio.to_thread(
                ensure_relationship_candidates,
                script_id,
                tag_set_ver=tag_set_ver,
                min_cooccurrence=1,
                top_k=30,
                persist=True,
                engine=default_engine,
            )
        async with counter_lock:
            counter["done"] += 1
            done = counter["done"]
        print(
            f"[freeze] script={script_id[:12]} {done}/{total} elapsed={int(time.time() - sub_t0)}s",
            flush=True,
        )

    await asyncio.gather(*(_freeze_one(sid) for sid in script_ids))


# ============================================================
# 第二层样本池：每个 scope 从全局池采样固定 N 个单元
# ============================================================


def _pool_episode(script_ids: list[str]) -> list[str]:
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
                    """
                ),
                {"sid": sid},
            ).mappings().all()
        for row in rows:
            out.append(f"{sid}::ep::{int(row['episode_no'])}")
    return out


def _pool_plot_unit(script_ids: list[str]) -> list[str]:
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
                    """
                ),
                {"sid": sid},
            ).mappings().all()
        out.extend([str(row["id"]) for row in rows])
    return out


def _pool_character(script_ids: list[str]) -> list[str]:
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
                    """
                ),
                {"sid": sid},
            ).mappings().all()
        out.extend([str(row["id"]) for row in rows])
    return out


def _pool_relationship(script_ids: list[str], tag_set_ver: str) -> list[str]:
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
                    """
                ),
                {"sid": sid, "ver": tag_set_ver},
            ).mappings().all()
        out.extend([str(row["id"]) for row in rows])
    return out


def _sample_targets(
    scope: str,
    script_ids: list[str],
    tag_set_ver: str,
    *,
    sample_size: int,
    seed: int,
) -> list[str]:
    """从 scope 全局池里采样 N 个单元。script 总池 ≤ N 全跑；其他 scope 用 seeded RNG 抽样。"""
    if scope == "script":
        return list(script_ids)
    if scope == "episode":
        pool = _pool_episode(script_ids)
    elif scope == "plot_unit":
        pool = _pool_plot_unit(script_ids)
    elif scope == "character":
        pool = _pool_character(script_ids)
    elif scope == "relationship":
        pool = _pool_relationship(script_ids, tag_set_ver)
    else:
        pool = list(script_ids)
    if len(pool) <= sample_size:
        return pool
    rng = random.Random(seed)
    return rng.sample(pool, sample_size)


# ============================================================
# Mock extractor（dev fallback：当目标 scope 没有真实 extractor 注册时）
# ============================================================


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
    bundles = _all_bundles(tag_set_ver)
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
        for bundle in _all_bundles(tag_set_ver, scope=scope):
            for dim in bundle.dims:
                dims_filter.add(dim)
        if scope in cfg.scope_to_dims:
            for dim_cfg in cfg.scope_to_dims.get(scope, ()):
                dims_filter.add(dim_cfg.dim)
    return dims_filter


# ============================================================
# 第二层：并发版的标签值稳定性
# ============================================================


async def _run_tag_value_for_bundle_concurrent(
    task: BundleStabilityTask,
    *,
    seed: int,
    n_repeats: int,
    exp_dir: ExperimentDir,
    use_cache: bool,
    aimd: AIMD,
    progress_tick: "asyncio.Lock | None" = None,
    progress_state: dict | None = None,
    log_label: str = "",
) -> dict[str, RunResult]:
    if not task.dims:
        return {}
    extractor = ExtractorRegistry.get(task.tag_set_ver, task.scope)
    work: list[tuple[str, int]] = [
        (target_id, rep) for rep in range(n_repeats) for target_id in task.targets
    ]
    by_rep_by_dim: dict[str, dict[int, dict[str, str]]] = {
        dim: {rep: {} for rep in range(n_repeats)} for dim in task.dims
    }
    prompt_dim = task.dims[0]

    async def _one(target_id: str, rep: int) -> None:
        script_id = _target_script_id(task.scope, target_id)
        existing_by_dim = {
            dim: exp_dir.tag_value_map(script_id, task.scope, dim, rep) for dim in task.dims
        }
        if all(target_id in existing_by_dim[dim] for dim in task.dims):
            for dim in task.dims:
                by_rep_by_dim[dim][rep][target_id] = str(existing_by_dim[dim][target_id])
            await _tick_progress(progress_tick, progress_state, log_label, target_id, rep)
            return

        prompt_ver = get_prompt_ver(task.tag_set_ver, prompt_dim, variant="a")
        input_hash = _hash_payload(
            {
                "target_id": target_id,
                "tag_set_ver": task.tag_set_ver,
                "bundle": task.bundle_id,
                "seed": seed,
                "variant": "a",
                "prompt_ver": prompt_ver,
            }
        )
        started = time.perf_counter()

        async def _call() -> dict:
            try:
                return await extractor(target_id, task.tag_set_ver, prompt_ver, seed, "a", use_cache=use_cache)
            except TypeError:
                return await extractor(target_id, task.tag_set_ver, prompt_ver, seed, "a")

        try:
            payload = await aimd_call(aimd, _call)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            for dim in task.dims:
                await asyncio.to_thread(
                    _persist_run,
                    task=StabilityTask(task.tag_set_ver, dim, task.scope, []),
                    scope_id=target_id,
                    prompt_ver=get_prompt_ver(task.tag_set_ver, dim, variant="a"),
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
            output_hash = _hash_payload({"value": value})
            dim_prompt_ver = get_prompt_ver(task.tag_set_ver, dim, variant="a")
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
            by_rep_by_dim[dim][rep][target_id] = value
        await _tick_progress(progress_tick, progress_state, log_label, target_id, rep)

    await asyncio.gather(*(_one(t, r) for t, r in work))

    results: dict[str, RunResult] = {}
    for dim in task.dims:
        result = RunResult(task=StabilityTask(task.tag_set_ver, dim, task.scope, list(task.targets)))
        for r in range(n_repeats):
            result.intra_runs.append(
                RunTrace(run_type="intra", run_key=f"rep:{r}", target_values=dict(by_rep_by_dim[dim][r]))
            )
        results[dim] = result
    return results


async def _tick_progress(
    lock: "asyncio.Lock | None",
    state: dict | None,
    label: str,
    target_id: str,
    rep: int,
) -> None:
    if lock is None or state is None:
        return
    async with lock:
        state["completed"] = state.get("completed", 0) + 1
        completed = state["completed"]
        total = state.get("total", 0)
        # 每 5% 或 25 步打一行，避免日志过密
        step = max(1, min(25, max(1, total // 20)))
        if completed % step == 0 or completed == total:
            print(
                f"[tag] {label} target={target_id[:12]} rep={rep + 1} progress={completed}/{total}",
                flush=True,
            )


# ============================================================
# CLI
# ============================================================


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run script-side tag stability experiment (Batch5.5).",
        epilog=(
            "Env sanity: SM_LLM_TYPE=dashscope / "
            "DASHSCOPE_MODEL_NAME=qwen3-max / "
            "SM_LLM_MODEL_AUX=qwen3-max / "
            "SM_STABILITY_DISABLE_CACHE=1"
        ),
    )
    parser.add_argument("--tag-set", required=True, dest="tag_set")
    parser.add_argument("--source-dir", default=str(_DEFAULT_SOURCE_DIR), help="script dataset directory")
    parser.add_argument("--n-scripts", type=int, default=0, help="limit scripts. 0 means all")
    parser.add_argument("--n-repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--sample-size",
        type=int,
        default=50,
        help="非-script scope 每个 scope 采样多少个单元（script 总池 ≤ 50 全跑）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=16,
        help="AIMD 起始并发；429 时降到 lo=4，5min 无 429 升档，上限 hi=64",
    )
    parser.add_argument(
        "--concurrency-lo",
        type=int,
        default=4,
        help="AIMD 并发下限",
    )
    parser.add_argument(
        "--concurrency-hi",
        type=int,
        default=64,
        help="AIMD 并发上限（默认 64）",
    )
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
        "--include-segmentation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="run segmentation stability layer (recommended on)",
    )
    parser.add_argument("--retry-budget", type=int, default=1, help="reserved retry budget for failed targets")
    return parser.parse_args()


async def _run_segmentation_stability(
    *,
    script_ids: list[str],
    n_repeats: int,
    seed: int,
    tag_set_ver: str,
    exp_dir: ExperimentDir,
    aimd: AIMD,
) -> list[Any]:
    """第一层并发：(script, rep) 为单元，asyncio.gather 调度。"""
    work: list[tuple[str, int]] = [(sid, r) for sid in script_ids for r in range(n_repeats)]
    total = len(work)
    state = {"completed": 0, "total": total}
    lock = asyncio.Lock()
    await exp_dir.update_progress("segmentation_stability", completed=0, total=total)
    print(f"[stage] segmentation_stability start: {total} units (scripts={len(script_ids)} × repeats={n_repeats})", flush=True)
    t0 = time.time()

    async def _one(sid: str, rep: int) -> None:
        await aimd_call(
            aimd,
            lambda: run_one_segmentation_step(
                sid,
                rep,
                seed=seed,
                tag_set_ver=tag_set_ver,
                exp_dir=exp_dir,
                engine=default_engine,
            ),
        )
        async with lock:
            state["completed"] += 1
            completed = state["completed"]
        print(
            f"[seg] script={sid[:12]} rep={rep + 1}/{n_repeats} progress={completed}/{total}",
            flush=True,
        )
        if completed % max(1, total // 10) == 0 or completed == total:
            await exp_dir.update_progress("segmentation_stability", completed=completed, total=total)

    await asyncio.gather(*(_one(sid, rep) for sid, rep in work))
    await exp_dir.update_progress("segmentation_stability", completed=total, total=total)

    reports = [aggregate_segmentation(sid, n_repeats, exp_dir=exp_dir, engine=default_engine) for sid in script_ids]
    exp_dir.save_aggregated_segmentation(reports)
    elapsed = int(time.time() - t0)
    print(f"[stage] segmentation_stability done in {elapsed}s ({total} units)", flush=True)
    return reports


async def _run_tag_value_stability(
    *,
    scopes: list[str],
    script_ids: list[str],
    tag_set_ver: str,
    sample_size: int,
    seed: int,
    n_repeats: int,
    dims_filter: set[str],
    exp_dir: ExperimentDir,
    aimd: AIMD,
    use_cache: bool,
) -> dict[str, Any]:
    samples = exp_dir.load_samples()

    run_results: dict[str, Any] = {}
    for scope in scopes:
        if scope in samples and samples[scope]:
            targets = samples[scope]
            print(f"[stage] tag_value_stability scope={scope} reuse samples n={len(targets)}", flush=True)
        else:
            targets = _sample_targets(scope, script_ids, tag_set_ver, sample_size=sample_size, seed=seed)
            samples[scope] = targets
            exp_dir.save_samples(samples)
            print(f"[stage] tag_value_stability scope={scope} sample n={len(targets)}", flush=True)
        if not targets:
            await exp_dir.update_progress("tag_value_stability", completed=0, total=0, scope=scope)
            continue

        scope_bundles: list[tuple[Any, list[str]]] = []
        for bundle in _all_bundles(tag_set_ver, scope=scope):
            dims = [dim for dim in bundle.dims if dim in dims_filter]
            if dims:
                scope_bundles.append((bundle, dims))
        if not scope_bundles:
            continue

        scope_t0 = time.time()
        total = len(targets) * n_repeats * len(scope_bundles)
        state = {"completed": 0, "total": total}
        lock = asyncio.Lock()
        await exp_dir.update_progress("tag_value_stability", completed=0, total=total, scope=scope)

        async def _run_bundle(bundle: Any, dims: list[str]) -> dict[str, Any]:
            task = BundleStabilityTask(
                tag_set_ver=tag_set_ver,
                bundle_id=bundle.id,
                scope=scope,
                dims=dims,
                targets=targets,
            )
            return await _run_tag_value_for_bundle_concurrent(
                task,
                seed=seed,
                n_repeats=n_repeats,
                exp_dir=exp_dir,
                use_cache=use_cache,
                aimd=aimd,
                progress_tick=lock,
                progress_state=state,
                log_label=f"scope={scope} bundle={bundle.id}",
            )

        per_bundle = await asyncio.gather(*(_run_bundle(bundle, dims) for bundle, dims in scope_bundles))
        for bundle_result in per_bundle:
            run_results.update(bundle_result)
        await exp_dir.update_progress("tag_value_stability", completed=state["completed"], total=total, scope=scope)
        elapsed = int(time.time() - scope_t0)
        dims_count = sum(len(dims) for _, dims in scope_bundles)
        print(
            f"[stage] tag_value_stability scope={scope} done in {elapsed}s "
            f"(bundles={len(scope_bundles)}, dims={dims_count}, samples={len(targets)}, calls={state['completed']}/{total})",
            flush=True,
        )

    return run_results


async def _main_async() -> None:
    args = _parse_args()
    _preflight(args)
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
        print(f"[stage] resume run_id={exp_dir.run_id} scripts={len(script_ids)}", flush=True)
    else:
        ingest_summary = ingest_dataset(
            dataset_dir=Path(args.source_dir),
            user_id=args.user_id,
            skip_unsupported=True,
            limit=args.n_scripts if args.n_scripts > 0 else None,
            summary_output=_DEFAULT_INGEST_SUMMARY,
        )
        script_ids = [str(row["script_id"]) for row in ingest_summary.get("ok", [])]
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
        await exp_dir.update_stage("ingest", "done")
        failed_rows = ingest_summary.get("failed") if isinstance(ingest_summary, dict) else None
        if isinstance(failed_rows, list) and failed_rows:
            failed_path = exp_dir.run_dir / "failed.json"
            failed_path.write_text(json.dumps({"failed": failed_rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"[stage] ingest done: run_id={exp_dir.run_id} scripts={len(script_ids)} "
            f"sample_size={args.sample_size} concurrency={args.concurrency}",
            flush=True,
        )

    for scope in scopes:
        if not ExtractorRegistry.has(args.tag_set, scope):
            ExtractorRegistry.register(args.tag_set, scope, _mock_extractor)

    aimd = AIMD(start=args.concurrency, lo=args.concurrency_lo, hi=args.concurrency_hi)
    ramp_stop = asyncio.Event()
    ramp_task = asyncio.create_task(aimd_ramp_watcher(aimd, ramp_stop))

    try:
        with _cache_env(bool(args.disable_cache)):
            if bool(args.include_segmentation) and exp_dir.manifest.get("stages", {}).get("segmentation_stability") != "done":
                await exp_dir.update_stage("segmentation_stability", "in_progress")
                await _run_segmentation_stability(
                    script_ids=script_ids,
                    n_repeats=args.n_repeats,
                    seed=args.seed,
                    tag_set_ver=args.tag_set,
                    exp_dir=exp_dir,
                    aimd=aimd,
                )
                await exp_dir.update_stage("segmentation_stability", "done")

            if exp_dir.manifest.get("stages", {}).get("freeze") != "done":
                print(f"[stage] freeze: bootstrap plot_units/characters/relationships for {len(script_ids)} scripts", flush=True)
                t0 = time.time()
                await _bootstrap_structures(
                    script_ids=script_ids,
                    tag_set_ver=args.tag_set,
                    scopes=scopes,
                    seed=args.seed,
                    aimd=aimd,
                )
                await exp_dir.update_stage("freeze", "done")
                print(f"[stage] freeze done in {int(time.time() - t0)}s", flush=True)

            await exp_dir.update_stage("tag_value_stability", "in_progress")
            run_results = await _run_tag_value_stability(
                scopes=scopes,
                script_ids=script_ids,
                tag_set_ver=args.tag_set,
                sample_size=args.sample_size,
                seed=args.seed,
                n_repeats=args.n_repeats,
                dims_filter=dims_filter,
                exp_dir=exp_dir,
                aimd=aimd,
                use_cache=not args.disable_cache,
            )
            reports = aggregate(run_results)
            exp_dir.save_aggregated_tag_value(reports)
            write_markdown(
                reports,
                str(exp_dir.run_dir / "aggregated" / "tag_value.md"),
                tag_set_ver=args.tag_set,
                split="full",
            )
            await exp_dir.update_stage("tag_value_stability", "done")
    finally:
        ramp_stop.set()
        try:
            await ramp_task
        except Exception:
            pass

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
                "aimd_stats": aimd.stats(),
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
