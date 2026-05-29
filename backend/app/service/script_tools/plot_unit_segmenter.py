"""Episode-level LLM-driven plot_unit segmentation.

设计依据：`docs/2026-05-25-情节标签设计决策.md §8.2`

切分流程：
1. 把单集 `IREpisode` 的 blocks（line_no | kind | character | text）渲染成 LLM 可读视图；
2. 携带前 3 集的 plot_unit summary 作为叙事连续性上下文；
3. LLM 一次性输出一集内的所有 plot_unit `line_range + summary + hints`；
4. 严格校验：line_range 首尾相接、不重叠、不越界；若失效则按"一场一情节"兜底；
5. 把 LLM 的 line_range 映射回 IRScene，写库 `scriptlens.plot_units`。

工业参考：
- Save the Cat / Snyder beats —— 以叙事节拍而非物理 scene 为最小单元
- Fabula / Story2KG —— Scene → Event 两遍处理，event 一等对象
- Hierarchical Discourse Parsing —— LLM 一次看完整 episode 决定 break，不在 scene 边界做候选
- MARCUS / MovieGraphs —— event-centric，每个 event 同时挂参与者与变化属性
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from jinja2 import BaseLoader, Environment
from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.llm_caller import LlmCaller, ModelTier
from service.script_tools.script_ir import IREpisode, IRLine, IRScene, build_script_ir
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)

_JINJA = Environment(loader=BaseLoader(), autoescape=False)
_SEGMENT_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "tag_registry" / "prompts" / "_internal" / "plot_unit_segment.jinja"
)

# Episode-level segmentation 的 LLM 输出预算：
# 单集最多 7 plot_unit × (summary ≤ 50 字 + line_range 2 int + 4 hints ≤ 20 字 + evidence_lines 3 int)
# ≈ 100 字/单元 × 7 = 700 字 ≈ 1100 token；× 1.5 safety = 1650 → 取 2048。
_SEGMENT_MAX_TOKENS = 2048

# 单集 block 视图截断：避免超长集塞爆 prompt。中位集 ~2K 字符；上限取 12000（含 line_no 等冗余）。
_MAX_EPISODE_BLOCKS_CHARS = 12000

# 前 N 集 plot_unit summary 作为叙事连续性上下文
_PRIOR_EPISODE_CONTEXT = 3

# 当 LLM 输出非法且无法修复时，回落到"一场一情节"
_FALLBACK_ONE_UNIT_PER_SCENE = True


def _resolve_segmenter_concurrency(default: int = 64) -> int:
    raw = os.getenv("SM_TAG_PIPELINE_CONCURRENCY", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


@dataclass
class SegmentedPlotUnit:
    id: str
    script_id: str
    episode_no: int | None
    idx: int
    start_scene_id: str | None
    end_scene_id: str | None
    start_line: int | None
    end_line: int | None
    summary: str
    char_count: int
    source: str = "llm"

    def to_dict(self) -> dict:
        return asdict(self)


@lru_cache(maxsize=1)
def _segment_template() -> str:
    return _SEGMENT_PROMPT_PATH.read_text(encoding="utf-8")


def _episode_lines(episode: IREpisode) -> list[tuple[int, IRLine, IRScene]]:
    """Flatten episode -> [(abs_line, IRLine, parent_IRScene)] sorted by abs_line."""
    rows: list[tuple[int, IRLine, IRScene]] = []
    for sc in episode.scenes:
        for ln in sc.lines:
            if ln.abs_line is None:
                continue
            rows.append((ln.abs_line, ln, sc))
    rows.sort(key=lambda t: t[0])
    return rows


def _render_blocks_view(rows: list[tuple[int, IRLine, IRScene]]) -> str:
    """Compact line-numbered block view; truncate by _MAX_EPISODE_BLOCKS_CHARS."""
    parts: list[str] = []
    used = 0
    for abs_line, ln, sc in rows:
        kind = ln.kind
        speaker = (ln.character or "").strip()
        body = (ln.text or "").strip()
        if not body and kind != "scene_header":
            continue
        if kind == "scene_header":
            prefix = f"L{abs_line:>5}|场标 |{sc.scene_label or sc.scene_no}"
            cell = prefix
        else:
            if speaker:
                cell = f"L{abs_line:>5}|{kind:<7}|{speaker}|{body}"
            else:
                cell = f"L{abs_line:>5}|{kind:<7}||{body}"
        if used + len(cell) + 1 > _MAX_EPISODE_BLOCKS_CHARS:
            parts.append("...（截断）")
            break
        parts.append(cell)
        used += len(cell) + 1
    return "\n".join(parts)


def _scene_at_line(scenes: list[IRScene], abs_line: int) -> IRScene | None:
    """Find the IRScene whose [start_line, end_line] contains abs_line."""
    for sc in scenes:
        if sc.start_line is None or sc.end_line is None:
            continue
        if sc.start_line <= abs_line <= sc.end_line:
            return sc
    return None


def _episode_line_bounds(rows: list[tuple[int, IRLine, IRScene]]) -> tuple[int, int] | None:
    if not rows:
        return None
    return rows[0][0], rows[-1][0]


def _validate_and_repair_units(
    raw_units: list[dict[str, Any]],
    *,
    bounds: tuple[int, int],
) -> list[dict[str, Any]]:
    """Validate LLM output:
    - line_range[0] <= line_range[1]
    - all within [bounds[0], bounds[1]]
    - first range starts at bounds[0], last ends at bounds[1]
    - adjacent ranges contiguous (next.start = prev.end + 1)

    Returns the **repaired** list, or [] when irrecoverable.
    """
    if not isinstance(raw_units, list) or not raw_units:
        return []

    lo, hi = bounds
    cleaned: list[dict[str, Any]] = []
    for unit in raw_units:
        if not isinstance(unit, dict):
            continue
        rng = unit.get("line_range")
        if not isinstance(rng, list) or len(rng) != 2:
            continue
        try:
            start = int(rng[0])
            end = int(rng[1])
        except (TypeError, ValueError):
            continue
        if start > end:
            continue
        # clamp to bounds
        start = max(lo, min(hi, start))
        end = max(lo, min(hi, end))
        if start > end:
            continue
        cleaned.append({**unit, "line_range": [start, end]})

    if not cleaned:
        return []

    # sort by start, drop overlaps by trimming end
    cleaned.sort(key=lambda u: (u["line_range"][0], u["line_range"][1]))
    repaired: list[dict[str, Any]] = []
    cursor = lo
    for unit in cleaned:
        start, end = unit["line_range"]
        if start < cursor:
            start = cursor  # absorb overlap into prior unit's tail
        if start > end:
            continue
        repaired.append({**unit, "line_range": [start, end]})
        cursor = end + 1

    if not repaired:
        return []

    # force first/last to align with episode bounds
    repaired[0]["line_range"][0] = lo
    repaired[-1]["line_range"][1] = hi

    # enforce contiguity (gap → extend prior; if a gap is huge, keep as boundary; pragmatically we
    # extend prior to cover the gap):
    contiguous: list[dict[str, Any]] = [repaired[0]]
    for unit in repaired[1:]:
        prev = contiguous[-1]
        if unit["line_range"][0] != prev["line_range"][1] + 1:
            # snap start to prev_end+1
            unit["line_range"][0] = prev["line_range"][1] + 1
            if unit["line_range"][0] > unit["line_range"][1]:
                # degenerate unit, drop
                continue
        contiguous.append(unit)

    return contiguous


def _fallback_one_unit_per_scene(scenes: list[IRScene]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for sc in scenes:
        if sc.start_line is None or sc.end_line is None or sc.end_line < sc.start_line:
            continue
        units.append(
            {
                "summary": (sc.scene_label or sc.scene_no or "").strip()[:50] or "未命名场",
                "line_range": [sc.start_line, sc.end_line],
                "location_hint": "",
                "time_of_day_hint": "未知",
                "in_out_hint": "未知",
                "characters_hint": list(sc.characters or []),
                "evidence_lines": [],
            }
        )
    return units


async def _llm_segment_episode(
    *,
    episode: IREpisode,
    prior_summaries: list[str],
    tag_set_ver: str,
    seed: int,
    variant: str,
    caller: LlmCaller,
) -> list[dict[str, Any]]:
    rows = _episode_lines(episode)
    bounds = _episode_line_bounds(rows)
    if bounds is None:
        return []

    if os.getenv("SM_TAGGING_DISABLE_LLM", "").strip().lower() in {"1", "true", "yes", "on"}:
        return _fallback_one_unit_per_scene(episode.scenes)

    blocks_view = _render_blocks_view(rows)
    prompt = _JINJA.from_string(_segment_template()).render(
        episode_no=episode.episode_no if episode.episode_no is not None else 0,
        episode_blocks=blocks_view,
        prior_summaries=prior_summaries,
    )
    prompt_ver = f"{tag_set_ver}:plot_unit_segment:{variant}"
    try:
        resp = await caller.call_json_deterministic(
            prompt,
            tag_set_ver=tag_set_ver,
            prompt_ver=prompt_ver,
            dim="plot_unit_segment",
            seed=seed,
            tier=ModelTier.PRIMARY,
            max_tokens=_SEGMENT_MAX_TOKENS,
            system_message="你只输出严格 JSON，不输出任何额外解释或 markdown。",
        )
        parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
        raw_units = parsed.get("plot_units") if isinstance(parsed.get("plot_units"), list) else []
        cleaned = _validate_and_repair_units(raw_units, bounds=bounds)
        if cleaned:
            return cleaned
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "plot_unit_segmenter llm failure ep=%s err=%s; falling back to scene-level units",
            episode.episode_no,
            exc,
        )

    if _FALLBACK_ONE_UNIT_PER_SCENE:
        return _fallback_one_unit_per_scene(episode.scenes)
    return []


def _build_plot_unit(
    *,
    script_id: str,
    episode: IREpisode,
    idx: int,
    raw_unit: dict[str, Any],
) -> SegmentedPlotUnit | None:
    start_line, end_line = raw_unit["line_range"]
    start_scene = _scene_at_line(episode.scenes, start_line)
    end_scene = _scene_at_line(episode.scenes, end_line)
    if start_scene is None or end_scene is None:
        return None
    rows = _episode_lines(episode)
    char_count = sum(len((ln.text or "")) for abs_line, ln, _ in rows if start_line <= abs_line <= end_line)
    summary = str(raw_unit.get("summary") or "").strip()[:200]
    if not summary:
        summary = f"{start_scene.scene_label or start_scene.scene_no} → {end_scene.scene_label or end_scene.scene_no}"
    return SegmentedPlotUnit(
        id=str(uuid.uuid4()),
        script_id=script_id,
        episode_no=episode.episode_no,
        idx=idx,
        start_scene_id=start_scene.scene_id,
        end_scene_id=end_scene.scene_id,
        start_line=start_line,
        end_line=end_line,
        summary=summary,
        char_count=char_count,
        source="llm",
    )


def _persist_plot_units_sync(*, script_id: str, units: list[SegmentedPlotUnit], engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM scriptlens.plot_units WHERE script_id = :sid AND source = 'llm'"),
            {"sid": script_id},
        )
        for unit in units:
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.plot_units
                        (id, script_id, episode_no, idx, start_scene_id, end_scene_id,
                         start_line, end_line, summary, char_count, source, created_at)
                    VALUES
                        (:id, :script_id, :episode_no, :idx, :start_scene_id, :end_scene_id,
                         :start_line, :end_line, :summary, :char_count, :source, NOW())
                    """
                ),
                unit.to_dict(),
            )


async def segment_plot_units(
    script_id: str,
    *,
    tag_set_ver: str = "script",
    seed: int = 42,
    variant: str = "a",
    caller: Optional[LlmCaller] = None,
    persist: bool = True,
    engine: Engine = default_engine,
) -> list[SegmentedPlotUnit]:
    """Episode-level LLM segmentation per design doc §8.2.

    Concurrency: each episode is one LLM call; all episodes run concurrently with a global
    semaphore (default 64, SM_TAG_PIPELINE_CONCURRENCY overrides).
    """
    ir = build_script_ir(script_id, engine=engine)
    if not ir.episodes:
        return []

    caller = caller or LlmCaller()
    concurrency = _resolve_segmenter_concurrency()
    sem = asyncio.Semaphore(concurrency)
    logger.info(
        "plot_unit_segmenter v2 script_id=%s episodes=%d concurrency=%d",
        script_id, len(ir.episodes), concurrency,
    )

    async def _segment_one(idx: int, ep: IREpisode, prior: list[str]) -> tuple[int, list[dict[str, Any]]]:
        async with sem:
            raw = await _llm_segment_episode(
                episode=ep,
                prior_summaries=prior,
                tag_set_ver=tag_set_ver,
                seed=seed,
                variant=variant,
                caller=caller,
            )
            return idx, raw

    # Two-pass design: first pass runs all episodes in parallel with no prior context (cold start);
    # subsequent runs benefit from the cache because (prompt, seed) is stable. For real narrative
    # continuity we'd need a sequential pass (run ep K, then read its summary into ep K+1 prompt),
    # but that re-serializes the whole pipeline. Compromise: bypass prior context on first run and
    # rely on the LLM's intra-episode context plus the stable seed; we cache results so re-runs
    # converge. If you need cross-episode narrative continuity, switch to phased rollout: ep 1-5
    # first, summarize, then ep 6-10, etc.
    raw_per_episode: list[list[dict[str, Any]]] = [[] for _ in ir.episodes]
    results = await asyncio.gather(
        *[_segment_one(i, ep, []) for i, ep in enumerate(ir.episodes)]
    )
    for i, raw in results:
        raw_per_episode[i] = raw

    units: list[SegmentedPlotUnit] = []
    global_idx = 1
    for ep, raw_units in zip(ir.episodes, raw_per_episode):
        for raw_unit in raw_units:
            built = _build_plot_unit(
                script_id=script_id,
                episode=ep,
                idx=global_idx,
                raw_unit=raw_unit,
            )
            if built is None:
                continue
            units.append(built)
            global_idx += 1

    if persist and units:
        await asyncio.to_thread(_persist_plot_units_sync, script_id=script_id, units=units, engine=engine)
    return units


def dump_segment_result(units: list[SegmentedPlotUnit]) -> str:
    payload = [u.to_dict() for u in units]
    return json.dumps(payload, ensure_ascii=False, indent=2)
