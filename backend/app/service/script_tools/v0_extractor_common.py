from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from jinja2 import BaseLoader, Environment
from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.script_ir import classify_line
from utils.database import engine as default_engine

_JINJA = Environment(loader=BaseLoader(), autoescape=False)


@dataclass
class PlotUnitContext:
    plot_unit_id: str
    script_id: str
    idx: int
    episode_no: int | None
    summary: str
    prev_summary: str
    next_summary: str
    full_text: str
    dialogue_text: str
    action_text: str
    start_scene_id: str | None
    end_scene_id: str | None


def render_prompt(template_text: str, **kwargs: Any) -> str:
    return _JINJA.from_string(template_text).render(**kwargs)


def stable_choice(values: list[str], key: str, default: str = "none") -> str:
    if not values:
        return default
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(values)
    return values[idx]


def resolve_script_id(script_ref: str, *, engine: Engine = default_engine) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.scripts
                WHERE id::text = :ref OR title = :ref
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"ref": script_ref},
        ).mappings().first()
    return str(row["id"]) if row else None


def _parse_plot_target(target_id: str) -> tuple[str, int] | None:
    if "::plot::" not in target_id:
        return None
    left, _, right = target_id.partition("::plot::")
    try:
        idx = int(right.strip())
    except ValueError:
        return None
    return left.strip(), idx


def resolve_plot_unit_id(target_id: str, *, engine: Engine = default_engine) -> str | None:
    parsed = _parse_plot_target(target_id)
    if parsed is None:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id::text AS id
                    FROM scriptlens.plot_units
                    WHERE id::text = :tid
                    LIMIT 1
                    """
                ),
                {"tid": target_id},
            ).mappings().first()
        return str(row["id"]) if row else None

    script_ref, idx = parsed
    script_id = resolve_script_id(script_ref, engine=engine)
    if script_id is None:
        return None
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.plot_units
                WHERE script_id = :sid AND idx = :idx
                LIMIT 1
                """
            ),
            {"sid": script_id, "idx": idx},
        ).mappings().first()
    return str(row["id"]) if row else None


def _scene_slice_text(
    *,
    script_id: str,
    start_scene_id: str | None,
    end_scene_id: str | None,
    engine: Engine,
) -> tuple[str, str, str]:
    with engine.connect() as conn:
        scenes = conn.execute(
            text(
                """
                SELECT id::text AS id, text, scene_label
                FROM scriptlens.scenes
                WHERE script_id = :sid
                ORDER BY episode_no NULLS LAST, scene_no, start_line
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    if not scenes:
        return "", "", ""

    start_idx = 0
    end_idx = len(scenes) - 1
    if start_scene_id:
        for i, row in enumerate(scenes):
            if row["id"] == start_scene_id:
                start_idx = i
                break
    if end_scene_id:
        for i, row in enumerate(scenes):
            if row["id"] == end_scene_id:
                end_idx = i
                break
    if end_idx < start_idx:
        start_idx, end_idx = end_idx, start_idx
    selected = scenes[start_idx : end_idx + 1]

    full_parts: list[str] = []
    dialogue_parts: list[str] = []
    action_parts: list[str] = []
    for row in selected:
        text_body = str(row["text"] or "")
        if row["scene_label"]:
            full_parts.append(f"[{row['scene_label']}]")
        for raw in text_body.splitlines():
            kind, character, body = classify_line(raw)
            body = (body or "").strip()
            if not body:
                continue
            full_parts.append(body)
            if kind in {"dialogue", "os", "vo"}:
                if character:
                    dialogue_parts.append(f"{character}: {body}")
                else:
                    dialogue_parts.append(body)
            elif kind in {"action", "scene_header", "stage_direction"}:
                action_parts.append(body)
    return (
        "\n".join(full_parts).strip(),
        "\n".join(dialogue_parts).strip(),
        "\n".join(action_parts).strip(),
    )


def load_plot_unit_context(target_id: str, *, engine: Engine = default_engine) -> PlotUnitContext | None:
    plot_unit_id = resolve_plot_unit_id(target_id, engine=engine)
    if plot_unit_id is None:
        return None
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id, script_id::text AS script_id, idx, episode_no,
                       summary, start_scene_id::text AS start_scene_id, end_scene_id::text AS end_scene_id
                FROM scriptlens.plot_units
                WHERE id = :pid
                LIMIT 1
                """
            ),
            {"pid": plot_unit_id},
        ).mappings().first()
        if not row:
            return None
        prev_row = conn.execute(
            text(
                """
                SELECT summary
                FROM scriptlens.plot_units
                WHERE script_id = :sid AND idx = :idx
                LIMIT 1
                """
            ),
            {"sid": row["script_id"], "idx": int(row["idx"]) - 1},
        ).mappings().first()
        next_row = conn.execute(
            text(
                """
                SELECT summary
                FROM scriptlens.plot_units
                WHERE script_id = :sid AND idx = :idx
                LIMIT 1
                """
            ),
            {"sid": row["script_id"], "idx": int(row["idx"]) + 1},
        ).mappings().first()

    full_text, dialogue_text, action_text = _scene_slice_text(
        script_id=row["script_id"],
        start_scene_id=row.get("start_scene_id"),
        end_scene_id=row.get("end_scene_id"),
        engine=engine,
    )
    summary = str(row.get("summary") or "").strip() or (full_text[:180] + ("…" if len(full_text) > 180 else ""))
    return PlotUnitContext(
        plot_unit_id=plot_unit_id,
        script_id=row["script_id"],
        idx=int(row["idx"]),
        episode_no=row.get("episode_no"),
        summary=summary,
        prev_summary=str((prev_row.get("summary") if prev_row else "") or ""),
        next_summary=str((next_row.get("summary") if next_row else "") or ""),
        full_text=full_text,
        dialogue_text=dialogue_text,
        action_text=action_text,
        start_scene_id=row.get("start_scene_id"),
        end_scene_id=row.get("end_scene_id"),
    )


def load_script_text(script_ref: str, *, engine: Engine = default_engine, max_chars: int = 6000) -> tuple[str | None, str]:
    script_id = resolve_script_id(script_ref, engine=engine)
    if script_id is None:
        # target might already be script_id-like but not in DB; return pseudo text
        return None, script_ref
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT summary
                FROM scriptlens.plot_units
                WHERE script_id = :sid
                ORDER BY idx
                LIMIT 400
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    chunks = [str(r.get("summary") or "").strip() for r in rows if str(r.get("summary") or "").strip()]
    text_blob = "\n".join(chunks).strip()
    if not text_blob:
        with engine.connect() as conn:
            scenes = conn.execute(
                text(
                    """
                    SELECT text
                    FROM scriptlens.scenes
                    WHERE script_id = :sid
                    ORDER BY episode_no NULLS LAST, scene_no, start_line
                    LIMIT 100
                    """
                ),
                {"sid": script_id},
            ).mappings().all()
        text_blob = "\n".join(str(x.get("text") or "") for x in scenes).strip()
    if len(text_blob) > max_chars:
        text_blob = text_blob[: max_chars - 1] + "…"
    return script_id, text_blob


def persist_script_tags(
    *,
    script_id: str,
    dim: str,
    values: list[str],
    tag_set_ver: str,
    prompt_ver: str,
    model_ver: str,
    source: str = "llm",
    confidence: float | None = None,
    evidence: dict | None = None,
    clear_existing: bool = False,
    engine: Engine = default_engine,
) -> None:
    evidence_json = json.dumps(evidence or {}, ensure_ascii=False)
    with engine.begin() as conn:
        if clear_existing:
            conn.execute(
                text(
                    """
                    DELETE FROM scriptlens.script_tags
                    WHERE script_id = :sid AND dim = :dim AND source = :source AND tag_set_ver = :ver
                    """
                ),
                {"sid": script_id, "dim": dim, "source": source, "ver": tag_set_ver},
            )
        for value in values:
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.script_tags
                        (id, script_id, dim, value, score, confidence, source, tag_set_ver,
                         prompt_ver, model_ver, run_id, evidence, created_at)
                    VALUES
                        (:id, :script_id, :dim, :value, NULL, :confidence, :source, :tag_set_ver,
                         :prompt_ver, :model_ver, NULL, CAST(:evidence AS jsonb), NOW())
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "script_id": script_id,
                    "dim": dim,
                    "value": value,
                    "confidence": confidence,
                    "source": source,
                    "tag_set_ver": tag_set_ver,
                    "prompt_ver": prompt_ver,
                    "model_ver": model_ver,
                    "evidence": evidence_json,
                },
            )


def persist_plot_unit_tags(
    *,
    plot_unit_id: str,
    values_by_dim: dict[str, str],
    tag_set_ver: str,
    prompt_ver: str,
    model_ver: str,
    source: str = "llm",
    confidence: float | None = None,
    evidence_by_dim: dict[str, dict] | None = None,
    clear_existing: bool = False,
    engine: Engine = default_engine,
) -> None:
    evidence_by_dim = evidence_by_dim or {}
    with engine.begin() as conn:
        if clear_existing:
            for dim in values_by_dim.keys():
                conn.execute(
                    text(
                        """
                        DELETE FROM scriptlens.plot_unit_tags
                        WHERE plot_unit_id = :pid AND dim = :dim AND source = :source AND tag_set_ver = :ver
                        """
                    ),
                    {"pid": plot_unit_id, "dim": dim, "source": source, "ver": tag_set_ver},
                )
        for dim, value in values_by_dim.items():
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.plot_unit_tags
                        (id, plot_unit_id, dim, value, score, confidence, source, tag_set_ver,
                         prompt_ver, model_ver, run_id, evidence, created_at)
                    VALUES
                        (:id, :plot_unit_id, :dim, :value, NULL, :confidence, :source, :tag_set_ver,
                         :prompt_ver, :model_ver, NULL, CAST(:evidence AS jsonb), NOW())
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "plot_unit_id": plot_unit_id,
                    "dim": dim,
                    "value": value,
                    "confidence": confidence,
                    "source": source,
                    "tag_set_ver": tag_set_ver,
                    "prompt_ver": prompt_ver,
                    "model_ver": model_ver,
                    "evidence": json.dumps(evidence_by_dim.get(dim) or {}, ensure_ascii=False),
                },
            )

