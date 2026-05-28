from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path
from typing import Optional

from jinja2 import BaseLoader, Environment
from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.llm_caller import LlmCaller, ModelTier
from service.script_tools.script_ir import IREpisode, IRScene, build_script_ir
from utils.database import engine as default_engine

_JINJA = Environment(loader=BaseLoader(), autoescape=False)
_BOUNDARY_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "tag_registry" / "prompts" / "_internal" / "boundary.jinja"
)


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


@dataclass
class _BoundaryDecision:
    keep: bool
    score: float
    reason: str = ""


@lru_cache(maxsize=1)
def _boundary_template() -> str:
    return _BOUNDARY_PROMPT_PATH.read_text(encoding="utf-8")


def _normalize_label(label: str) -> str:
    out = (label or "").strip().lower()
    out = out.replace(" ", "")
    out = out.replace("，", ",")
    return out


def _scene_characters(scene: IRScene) -> set[str]:
    chars = {c.strip() for c in (scene.characters or []) if c and c.strip()}
    for line in scene.lines:
        if line.character:
            chars.add(line.character.strip())
    return {c for c in chars if c}


def _char_change_ratio(prev: IRScene, cur: IRScene) -> float:
    a = _scene_characters(prev)
    b = _scene_characters(cur)
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return 1.0 - (inter / union)


def _stage_density(scene: IRScene) -> float:
    if not scene.lines:
        return 0.0
    stage_like = sum(1 for ln in scene.lines if ln.kind in {"stage_direction", "action", "scene_header"})
    return stage_like / max(len(scene.lines), 1)


def _candidate_boundary_strength(prev: IRScene, cur: IRScene) -> float:
    label_jump = 1.0 if _normalize_label(prev.scene_label) != _normalize_label(cur.scene_label) else 0.0
    char_jump = _char_change_ratio(prev, cur)
    stage_delta = abs(_stage_density(prev) - _stage_density(cur))
    return max(label_jump, char_jump, min(1.0, stage_delta * 1.8))


def _is_candidate_boundary(prev: IRScene, cur: IRScene) -> tuple[bool, float]:
    strength = _candidate_boundary_strength(prev, cur)
    return strength >= 0.45, strength


def _segment_preview(scenes: list[IRScene], start_idx: int, end_idx: int, max_chars: int = 700) -> str:
    parts: list[str] = []
    for sc in scenes[start_idx : end_idx + 1]:
        first_lines = [ln.text.strip() for ln in sc.lines if ln.text and ln.text.strip()][:3]
        snippet = " ".join(first_lines)
        cell = f"[{sc.scene_label or sc.scene_no}] {snippet}".strip()
        if cell:
            parts.append(cell)
    text_block = "\n".join(parts)
    if len(text_block) > max_chars:
        return text_block[: max_chars - 1] + "…"
    return text_block


def _build_summary(scenes: list[IRScene], start_idx: int, end_idx: int) -> str:
    preview = _segment_preview(scenes, start_idx, end_idx, max_chars=260)
    return preview or f"{scenes[start_idx].scene_label} -> {scenes[end_idx].scene_label}"


async def _llm_keep_boundary(
    *,
    prev_text: str,
    next_text: str,
    candidate_strength: float,
    tag_set_ver: str,
    seed: int,
    variant: str,
    caller: LlmCaller,
) -> _BoundaryDecision:
    default_keep = candidate_strength >= 0.8
    if os.getenv("SM_TAGGING_DISABLE_LLM", "").strip().lower() in {"1", "true", "yes", "on"}:
        score = candidate_strength if default_keep else 1.0 - candidate_strength
        return _BoundaryDecision(keep=default_keep, score=score, reason="llm_disabled")
    prompt = _JINJA.from_string(_boundary_template()).render(
        prev_text=prev_text,
        next_text=next_text,
    )
    prompt_ver = f"{tag_set_ver}:plot_unit_boundary_keep:{variant}"
    try:
        resp = await caller.call_json_deterministic(
            prompt,
            tag_set_ver=tag_set_ver,
            prompt_ver=prompt_ver,
            dim="plot_unit_boundary_keep",
            seed=seed,
            tier=ModelTier.MINI,
            max_tokens=256,
            system_message="你只输出合法 JSON，不输出额外解释。",
        )
        parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
        raw_keep = str(parsed.get("keep_boundary") or parsed.get("decision") or "").strip().lower()
        if raw_keep in {"keep", "true", "yes", "1"}:
            keep = True
        elif raw_keep in {"merge", "false", "no", "0"}:
            keep = False
        else:
            keep = default_keep
        score_raw = parsed.get("score")
        try:
            score = float(score_raw)
        except (TypeError, ValueError):
            score = 1.0 if keep else 0.0
        score = max(0.0, min(1.0, score))
        reason = str(parsed.get("reason") or "")
        return _BoundaryDecision(keep=keep, score=score, reason=reason[:80])
    except Exception:
        score = candidate_strength if default_keep else 1.0 - candidate_strength
        return _BoundaryDecision(keep=default_keep, score=score, reason="fallback_by_rule")


async def _segment_episode(
    episode: IREpisode,
    *,
    tag_set_ver: str,
    seed: int,
    variant: str,
    caller: LlmCaller,
    max_plot_units_per_episode: int,
) -> list[tuple[int, int]]:
    scenes = episode.scenes
    if not scenes:
        return []
    if len(scenes) == 1:
        return [(0, 0)]

    boundaries: list[int] = [0]
    boundary_scores: dict[int, float] = {}
    for i in range(1, len(scenes)):
        is_candidate, strength = _is_candidate_boundary(scenes[i - 1], scenes[i])
        if not is_candidate:
            continue
        decision = await _llm_keep_boundary(
            prev_text=_segment_preview(scenes, max(0, i - 2), i - 1),
            next_text=_segment_preview(scenes, i, min(len(scenes) - 1, i + 1)),
            candidate_strength=strength,
            tag_set_ver=tag_set_ver,
            seed=seed,
            variant=variant,
            caller=caller,
        )
        if decision.keep:
            boundaries.append(i)
            boundary_scores[i] = decision.score

    boundaries.append(len(scenes))
    boundaries = sorted(set(boundaries))

    # 每集最多 N 个 plot_unit：优先移除置信度最低的边界
    while len(boundaries) - 1 > max_plot_units_per_episode and len(boundaries) > 2:
        removable = boundaries[1:-1]
        if not removable:
            break
        drop = min(removable, key=lambda x: boundary_scores.get(x, 0.0))
        boundaries.remove(drop)

    spans: list[tuple[int, int]] = []
    for i in range(len(boundaries) - 1):
        start = boundaries[i]
        end = boundaries[i + 1] - 1
        if start <= end:
            spans.append((start, end))
    return spans


def _build_plot_unit(
    *,
    script_id: str,
    episode_no: int | None,
    idx: int,
    scenes: list[IRScene],
    start_idx: int,
    end_idx: int,
) -> SegmentedPlotUnit:
    seg_scenes = scenes[start_idx : end_idx + 1]
    start_scene = seg_scenes[0]
    end_scene = seg_scenes[-1]
    char_count = sum(len((ln.text or "")) for sc in seg_scenes for ln in sc.lines)
    return SegmentedPlotUnit(
        id=str(uuid.uuid4()),
        script_id=script_id,
        episode_no=episode_no,
        idx=idx,
        start_scene_id=start_scene.scene_id,
        end_scene_id=end_scene.scene_id,
        start_line=start_scene.start_line,
        end_line=end_scene.end_line,
        summary=_build_summary(scenes, start_idx, end_idx),
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
    tag_set_ver: str = "v0.1.0",
    seed: int = 42,
    variant: str = "a",
    max_plot_units_per_episode: int = 8,
    caller: Optional[LlmCaller] = None,
    persist: bool = True,
    engine: Engine = default_engine,
) -> list[SegmentedPlotUnit]:
    """Segment scenes into plot_units, optionally persisting to DB."""
    ir = build_script_ir(script_id, engine=engine)
    if not ir.episodes:
        return []

    caller = caller or LlmCaller()
    units: list[SegmentedPlotUnit] = []
    global_idx = 1
    for ep in ir.episodes:
        spans = await _segment_episode(
            ep,
            tag_set_ver=tag_set_ver,
            seed=seed,
            variant=variant,
            caller=caller,
            max_plot_units_per_episode=max_plot_units_per_episode,
        )
        for start_idx, end_idx in spans:
            units.append(
                _build_plot_unit(
                    script_id=script_id,
                    episode_no=ep.episode_no,
                    idx=global_idx,
                    scenes=ep.scenes,
                    start_idx=start_idx,
                    end_idx=end_idx,
                )
            )
            global_idx += 1

    if persist and units:
        await asyncio.to_thread(_persist_plot_units_sync, script_id=script_id, units=units, engine=engine)
    return units


def dump_segment_result(units: list[SegmentedPlotUnit]) -> str:
    payload = [u.to_dict() for u in units]
    return json.dumps(payload, ensure_ascii=False, indent=2)

