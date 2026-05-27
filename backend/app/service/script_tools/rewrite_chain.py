"""Rewrite chain v3: drive plan by improvement actions."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)

_DIMENSIONS_SIX = ("story", "character", "concept", "emotion", "pacing", "dialogue")
_DIM_LABELS_ZH = {
    "story": "故事力",
    "character": "人物力",
    "concept": "题材力",
    "emotion": "情感力",
    "pacing": "叙事力",
    "dialogue": "台词力",
}

_MAX_PLAN_STEPS = 12
_CONTEXT_WINDOW = 2
_SCENE_DIGEST_CHARS = 180
_CHARACTERS_TOP_N = 12


@dataclass
class PlanStep:
    scene_id: str
    scene_label: str
    episode_no: Optional[int]
    scene_no: Optional[str]
    target_dimensions: List[str]
    rationale: str
    expected_changes: str
    current_excerpt: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "scene_label": self.scene_label,
            "episode_no": self.episode_no,
            "scene_no": self.scene_no,
            "target_dimensions": list(self.target_dimensions),
            "rationale": self.rationale,
            "expected_changes": self.expected_changes,
            "current_excerpt": self.current_excerpt,
        }


@dataclass
class RewritePlan:
    dimensions: List[str]
    overall_summary: str
    steps: List[PlanStep] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimensions": list(self.dimensions),
            "overall_summary": self.overall_summary,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class RewriteResult:
    scene_id: str
    scene_label: str
    target_dimensions: List[str]
    original_text: str
    rewritten_text: str
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "scene_label": self.scene_label,
            "target_dimensions": list(self.target_dimensions),
            "original_text": self.original_text,
            "rewritten_text": self.rewritten_text,
            "rationale": self.rationale,
        }


def select_target_scenes(
    *,
    script_id: str,
    dimensions: List[str],
    max_scenes: int = _MAX_PLAN_STEPS,
    engine: Engine = default_engine,
) -> List[Dict[str, Any]]:
    """Select rewrite targets from latest scoring improvement actions."""
    dims = [dim for dim in dimensions if dim in _DIMENSIONS_SIX]
    if not dims:
        return []

    with engine.connect() as conn:
        run_row = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.scoring_runs
                WHERE script_id = :sid
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"sid": script_id},
        ).mappings().first()
        if run_row is None:
            return []
        run_id = str(run_row["id"])

        action_rows = conn.execute(
            text(
                """
                SELECT id::text AS id,
                       dimension,
                       signal_key,
                       issue,
                       target,
                       action_steps,
                       evidence_refs
                FROM scriptlens.scoring_improvement_actions
                WHERE script_id = :sid
                  AND run_id = :rid
                  AND dimension = ANY(:dims)
                ORDER BY created_at DESC
                """
            ),
            {"sid": script_id, "rid": run_id, "dims": dims},
        ).mappings().all()
        if not action_rows:
            return []

        scene_rows = conn.execute(
            text(
                """
                SELECT id::text AS id, episode_no, scene_no, scene_label, text
                FROM scriptlens.scenes
                WHERE script_id = :sid
                ORDER BY episode_no NULLS LAST, scene_no, start_line
                """
            ),
            {"sid": script_id},
        ).mappings().all()

    scene_index = {str(row["id"]): dict(row) for row in scene_rows}
    scene_order = {str(row["id"]): idx for idx, row in enumerate(scene_rows)}
    per_scene: dict[str, dict[str, Any]] = {}

    for row in action_rows:
        dimension = str(row.get("dimension") or "").strip()
        if dimension not in dims:
            continue
        evidence_refs = row.get("evidence_refs")
        if isinstance(evidence_refs, str):
            try:
                evidence_refs = json.loads(evidence_refs)
            except (TypeError, ValueError):
                evidence_refs = []
        if not isinstance(evidence_refs, list):
            evidence_refs = []

        scene_ids = _extract_scene_ids(evidence_refs)
        if not scene_ids:
            continue
        action_steps = row.get("action_steps")
        if isinstance(action_steps, str):
            try:
                action_steps = json.loads(action_steps)
            except (TypeError, ValueError):
                action_steps = []
        if not isinstance(action_steps, list):
            action_steps = []
        action_step_lines = [str(item).strip() for item in action_steps if str(item).strip()]
        issue = _first_sentence(str(row.get("issue") or "").strip(), max_len=80)
        fallback_target = _first_sentence(str(row.get("target") or "").strip(), max_len=120)
        expected_change = "；".join(action_step_lines[:2]) if action_step_lines else fallback_target
        for scene_id in scene_ids:
            scene = scene_index.get(scene_id)
            if scene is None:
                continue
            bucket = per_scene.setdefault(
                scene_id,
                {
                    "scene_id": scene_id,
                    "scene_label": str(scene.get("scene_label") or ""),
                    "episode_no": scene.get("episode_no"),
                    "scene_no": scene.get("scene_no"),
                    "text": str(scene.get("text") or ""),
                    "matched_dimensions": [],
                    "dim_reasons": {},
                    "expected_changes": [],
                    "_action_count": 0,
                },
            )
            if dimension not in bucket["matched_dimensions"]:
                bucket["matched_dimensions"].append(dimension)
            if issue:
                bucket["dim_reasons"][dimension] = issue
            if expected_change:
                bucket["expected_changes"].append(expected_change)
            bucket["_action_count"] += 1

    candidates = list(per_scene.values())
    if not candidates:
        return []

    dim_rank = {dim: idx for idx, dim in enumerate(_DIMENSIONS_SIX)}
    for item in candidates:
        item["matched_dimensions"].sort(key=lambda dim: dim_rank.get(dim, 999))
        item["expected_changes"] = _dedup_strs(item.get("expected_changes") or [])

    def _priority(item: dict[str, Any]) -> tuple[int, int, int]:
        return (
            len(item.get("matched_dimensions") or []),
            int(item.get("_action_count") or 0),
            -(scene_order.get(item.get("scene_id") or "", 10**9)),
        )

    candidates.sort(key=_priority, reverse=True)
    trimmed = candidates[: max(1, min(max_scenes, 30))]
    trimmed.sort(key=lambda item: scene_order.get(item.get("scene_id") or "", 10**9))
    for item in trimmed:
        item.pop("_action_count", None)
    return trimmed


async def propose_plan(
    *,
    script_id: str,
    dimensions: List[str],
    scenes: Optional[List[Dict[str, Any]]] = None,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> RewritePlan:
    """Build rewrite plan from action-driven targets.

    `caller` is retained for API compatibility, but v3 defaults to deterministic rule planning.
    """
    _ = caller
    dims = [dim for dim in dimensions if dim in _DIMENSIONS_SIX]
    if not dims:
        raise ValueError(
            f"dimensions must be a non-empty subset of {_DIMENSIONS_SIX}; got {dimensions!r}"
        )
    if scenes is None:
        scenes = select_target_scenes(script_id=script_id, dimensions=dims, engine=engine)
    if not scenes:
        return RewritePlan(
            dimensions=dims,
            overall_summary="当前维度暂无可执行的改写动作（缺 improvement_actions 或证据不足）。",
            steps=[],
        )

    steps: list[PlanStep] = []
    for scene in scenes:
        scene_dims = [dim for dim in (scene.get("matched_dimensions") or []) if dim in dims]
        if not scene_dims:
            scene_dims = dims[:1]
        reasons = scene.get("dim_reasons") or {}
        rationale_parts = [str(reasons.get(dim) or "").strip() for dim in scene_dims if str(reasons.get(dim) or "").strip()]
        rationale = "；".join(rationale_parts) if rationale_parts else "该场覆盖多个弱项信号，优先改写。"
        expected_candidates = scene.get("expected_changes") or []
        expected_changes = "；".join(str(item).strip() for item in expected_candidates[:2] if str(item).strip())
        if not expected_changes:
            expected_changes = "按改写动作补齐该场的冲突推进与情绪兑现。"
        excerpt = _truncate(str(scene.get("text") or "").replace("\r\n", "\n"), 200)
        steps.append(
            PlanStep(
                scene_id=str(scene.get("scene_id") or ""),
                scene_label=str(scene.get("scene_label") or ""),
                episode_no=scene.get("episode_no"),
                scene_no=str(scene.get("scene_no")) if scene.get("scene_no") is not None else None,
                target_dimensions=scene_dims,
                rationale=_truncate(rationale, 80),
                expected_changes=_truncate(expected_changes, 120),
                current_excerpt=excerpt,
            )
        )

    dim_text = "/".join(_DIM_LABELS_ZH.get(dim, dim) for dim in dims)
    summary = f"基于改写动作共生成 {len(steps)} 个步骤，重点修复 {dim_text} 的弱项信号。"
    return RewritePlan(dimensions=dims, overall_summary=summary, steps=steps)


_EXECUTE_PROMPT = """你是中文短剧资深编剧。请基于整剧上下文对目标场进行改写。

【整剧概要】
{script_overview}

【人物表】
{characters_block}

【前情场次摘要】
{prev_scenes_block}

【目标场原文】（{scene_label}）
---
{scene_text}
---

【后续场次摘要】
{next_scenes_block}

【目标改写维度】{target_dimensions_text}

【改写动作指令】
{expected_changes}

约束：
1. 只输出目标场的新文本，不改前后场。
2. 保持角色、世界观、核心事件连续性，不得引入新主线。
3. 字数与原文尽量同量级（允许上下浮动约 30%）。
4. 多维同时优化时优先保证主线推进与角色动机清晰。

输出严格 JSON：
{{
  "rewritten_text": "<改写后的整段场景文本>",
  "rationale": "<≤150字，说明主要改动及提分原因>"
}}"""


async def execute_plan_step(
    *,
    script_id: str,
    scene_id: str,
    target_dimensions: List[str],
    expected_changes: str = "",
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> RewriteResult:
    dims = [dim for dim in target_dimensions if dim in _DIMENSIONS_SIX]
    if not dims:
        raise ValueError(
            f"target_dimensions must be a non-empty subset of {_DIMENSIONS_SIX}; got {target_dimensions!r}"
        )

    ctx = _load_rewrite_context(scene_id, expected_script_id=script_id, engine=engine)
    if ctx is None:
        raise ValueError(f"scene_id {scene_id} not found in script {script_id}")
    scene = ctx["scene"]
    scene_text = str(scene.get("text") or "")
    if not scene_text.strip():
        raise ValueError(f"scene {scene_id} has empty text")

    if not expected_changes.strip():
        expected_changes = _load_scene_expected_changes(
            script_id=script_id,
            scene_id=scene_id,
            dimensions=dims,
            engine=engine,
        ) or "按目标维度修复弱项并提升可读性。"

    target_dims_text = " + ".join(f"{dim}（{_DIM_LABELS_ZH.get(dim, dim)}）" for dim in dims)
    prompt = _EXECUTE_PROMPT.format(
        script_overview=ctx["script_overview"],
        characters_block=ctx["characters_block"],
        prev_scenes_block=ctx["prev_scenes_block"],
        scene_label=scene.get("scene_label") or "",
        scene_text=scene_text,
        next_scenes_block=ctx["next_scenes_block"],
        target_dimensions_text=target_dims_text,
        expected_changes=expected_changes,
    )

    caller = caller or LlmCaller()
    try:
        resp = await caller.call_json(
            prompt,
            tier=ModelTier.PRIMARY,
            temperature=0.2,
            max_tokens=TokenBudget.REWRITE_EXCERPT,
        )
    except ScoreLLMError as exc:
        logger.warning("execute_plan_step LLM failed for scene %s: %s", scene_id, exc)
        raise

    parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    rewritten_text = str(parsed.get("rewritten_text") or "").strip()
    rationale = str(parsed.get("rationale") or "").strip()
    if not rewritten_text:
        raise ScoreLLMError(f"execute_plan_step: LLM returned empty rewritten_text for scene {scene_id}")

    return RewriteResult(
        scene_id=scene_id,
        scene_label=str(scene.get("scene_label") or ""),
        target_dimensions=dims,
        original_text=scene_text,
        rewritten_text=rewritten_text,
        rationale=rationale or "已按目标维度完成改写。",
    )


def _load_scene_expected_changes(
    *,
    script_id: str,
    scene_id: str,
    dimensions: list[str],
    engine: Engine = default_engine,
) -> str:
    with engine.connect() as conn:
        run_row = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.scoring_runs
                WHERE script_id = :sid
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"sid": script_id},
        ).mappings().first()
        if run_row is None:
            return ""
        run_id = str(run_row["id"])
        rows = conn.execute(
            text(
                """
                SELECT target, action_steps, evidence_refs
                FROM scriptlens.scoring_improvement_actions
                WHERE script_id = :sid
                  AND run_id = :rid
                  AND dimension = ANY(:dims)
                ORDER BY created_at DESC
                """
            ),
            {"sid": script_id, "rid": run_id, "dims": dimensions},
        ).mappings().all()
    changes: list[str] = []
    for row in rows:
        evidence_refs = row.get("evidence_refs")
        if isinstance(evidence_refs, str):
            try:
                evidence_refs = json.loads(evidence_refs)
            except (TypeError, ValueError):
                evidence_refs = []
        if not isinstance(evidence_refs, list):
            evidence_refs = []
        if scene_id not in _extract_scene_ids(evidence_refs):
            continue
        action_steps = row.get("action_steps")
        if isinstance(action_steps, str):
            try:
                action_steps = json.loads(action_steps)
            except (TypeError, ValueError):
                action_steps = []
        if isinstance(action_steps, list):
            changes.extend(str(step).strip() for step in action_steps if str(step).strip())
        target = str(row.get("target") or "").strip()
        if target:
            changes.append(target)
    deduped = _dedup_strs(changes)
    return "；".join(deduped[:3]) if deduped else ""


def _load_script_overview(script_id: str, *, engine: Engine = default_engine) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT report_json
                FROM scriptlens.reports
                WHERE script_id = :sid
                ORDER BY generated_at DESC
                LIMIT 1
                """
            ),
            {"sid": script_id},
        ).mappings().first()
    if row is None:
        return "（暂无诊断报告，仅基于上下文改写）"
    payload = row.get("report_json")
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    if not isinstance(payload, dict):
        return "（报告解析失败，仅基于上下文改写）"
    summary = str(payload.get("summary") or "").strip()
    decision = payload.get("decision") or {}
    reason = str(decision.get("one_sentence_reason") or "").strip() if isinstance(decision, dict) else ""
    parts = [part for part in (summary, reason) if part]
    return "\n".join(parts) if parts else "（暂无整剧概要）"


def _load_rewrite_context(
    scene_id: str,
    *,
    expected_script_id: str,
    engine: Engine = default_engine,
) -> Optional[Dict[str, Any]]:
    with engine.connect() as conn:
        target = conn.execute(
            text(
                """
                SELECT id::text AS id, script_id::text AS script_id,
                       episode_no, scene_no, scene_label, text
                FROM scriptlens.scenes
                WHERE id = :scene_id
                """
            ),
            {"scene_id": scene_id},
        ).mappings().first()
        if target is None:
            return None
        target_dict = dict(target)
        if target_dict.get("script_id") != expected_script_id:
            raise ValueError("scene_id does not belong to current script")

        all_scenes = conn.execute(
            text(
                """
                SELECT id::text AS id, episode_no, scene_no, scene_label,
                       LEFT(text, :digest_chars) AS digest
                FROM scriptlens.scenes
                WHERE script_id = :sid
                ORDER BY episode_no NULLS LAST, scene_no, start_line
                """
            ),
            {"sid": expected_script_id, "digest_chars": _SCENE_DIGEST_CHARS},
        ).mappings().all()

        character_rows = conn.execute(
            text(
                """
                SELECT character_name, COUNT(*) AS appearances
                FROM (
                    SELECT unnest(characters) AS character_name
                    FROM scriptlens.scenes
                    WHERE script_id = :sid
                ) t
                WHERE character_name IS NOT NULL AND character_name <> ''
                GROUP BY character_name
                ORDER BY appearances DESC, character_name ASC
                LIMIT :top_n
                """
            ),
            {"sid": expected_script_id, "top_n": _CHARACTERS_TOP_N},
        ).mappings().all()

    scenes_list = [dict(row) for row in all_scenes]
    target_idx = next((idx for idx, row in enumerate(scenes_list) if row.get("id") == scene_id), None)
    if target_idx is None:
        return None
    prev_window = scenes_list[max(0, target_idx - _CONTEXT_WINDOW) : target_idx]
    next_window = scenes_list[target_idx + 1 : target_idx + 1 + _CONTEXT_WINDOW]
    return {
        "scene": target_dict,
        "script_overview": _load_script_overview(expected_script_id, engine=engine),
        "characters_block": _format_characters([dict(row) for row in character_rows]),
        "prev_scenes_block": _format_window(prev_window) or "（无前情场次）",
        "next_scenes_block": _format_window(next_window) or "（无后续场次）",
    }


def _extract_scene_ids(evidence_refs: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for ref in evidence_refs:
        if isinstance(ref, dict):
            candidate = str(ref.get("scene_id") or "").strip()
            if not candidate:
                anchor = ref.get("anchor") if isinstance(ref.get("anchor"), dict) else {}
                candidate = str(anchor.get("scene_id") or "").strip()
            if not candidate:
                ref_id = str(ref.get("id") or "").strip()
                if ref_id.startswith("scene:"):
                    candidate = ref_id.split(":", 1)[1]
        else:
            candidate = str(ref or "").strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _dedup_strs(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _format_window(window: list[dict[str, Any]]) -> str:
    if not window:
        return ""
    return "\n".join(f"- {_scene_title(scene)}：{str(scene.get('digest') or '').strip().replace('\n', ' ')}" for scene in window)


def _format_characters(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "（暂无人物统计）"
    return "\n".join(
        f"- {row.get('character_name')}（出场 {row.get('appearances')} 场）" for row in rows
    )


def _scene_title(scene: dict[str, Any]) -> str:
    parts: list[str] = []
    ep = scene.get("episode_no")
    if ep is not None:
        parts.append(f"第{ep}集")
    sn = scene.get("scene_no")
    if sn is not None and str(sn).strip():
        parts.append(f"第{sn}场")
    label = scene.get("scene_label")
    if label:
        parts.append(f"《{label}》")
    return " ".join(parts) if parts else "未命名场"


def _first_sentence(text: str, *, max_len: int = 80) -> str:
    s = (text or "").strip()
    for sep in ("\n", "。", "；", "！", "?", "？"):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    return _truncate(s, max_len)


def _truncate(text: str, max_len: int) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"
