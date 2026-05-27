from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.v0_extractor_common import resolve_script_id
from utils.database import engine as default_engine


@dataclass
class EpisodeContext:
    script_id: str
    episode_no: int
    episode_text: str


@dataclass
class CharacterContext:
    character_id: str
    script_id: str
    canonical_name: str
    aliases: list[str]
    role: str | None
    character_text: str


@dataclass
class RelationshipContext:
    relationship_id: str
    script_id: str
    src_char_id: str
    dst_char_id: str
    src_name: str
    dst_name: str
    relationship_text: str


def _parse_episode_target(target_id: str) -> tuple[str, int] | None:
    if "::ep::" not in target_id:
        return None
    left, _, right = target_id.partition("::ep::")
    try:
        episode_no = int(right.strip())
    except ValueError:
        return None
    return left.strip(), episode_no


def load_episode_context(target_id: str, *, engine: Engine = default_engine, max_chars: int = 3000) -> EpisodeContext | None:
    parsed = _parse_episode_target(target_id)
    if parsed is None:
        return None
    script_ref, episode_no = parsed
    script_id = resolve_script_id(script_ref, engine=engine) or script_ref
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT scene_label, text
                FROM scriptlens.scenes
                WHERE script_id = :sid AND episode_no = :ep
                ORDER BY scene_no, start_line
                LIMIT 200
                """
            ),
            {"sid": script_id, "ep": episode_no},
        ).mappings().all()
    if not rows:
        return None
    blocks: list[str] = []
    for row in rows:
        label = str(row.get("scene_label") or "").strip()
        text_body = str(row.get("text") or "").strip()
        if not text_body:
            continue
        if label:
            blocks.append(f"[{label}]\n{text_body}")
        else:
            blocks.append(text_body)
    episode_text = "\n\n".join(blocks).strip()
    if len(episode_text) > max_chars:
        episode_text = episode_text[: max_chars - 1] + "…"
    return EpisodeContext(script_id=script_id, episode_no=episode_no, episode_text=episode_text)


def _parse_character_target(target_id: str) -> tuple[str, int] | None:
    if "::char::" not in target_id:
        return None
    left, _, right = target_id.partition("::char::")
    try:
        idx = int(right.strip())
    except ValueError:
        return None
    return left.strip(), idx


def _load_character_row_by_id(target_id: str, *, engine: Engine) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id, script_id::text AS script_id, canonical_name, aliases, role
                FROM scriptlens.character_entities
                WHERE id::text = :cid
                LIMIT 1
                """
            ),
            {"cid": target_id},
        ).mappings().first()
    return dict(row) if row else None


def _load_character_row_by_index(script_ref: str, idx: int, *, engine: Engine) -> dict | None:
    script_id = resolve_script_id(script_ref, engine=engine)
    if script_id is None:
        return None
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id, script_id::text AS script_id, canonical_name, aliases, role
                FROM scriptlens.character_entities
                WHERE script_id = :sid
                ORDER BY created_at, canonical_name
                OFFSET :offset LIMIT 1
                """
            ),
            {"sid": script_id, "offset": max(idx - 1, 0)},
        ).mappings().first()
    return dict(row) if row else None


def _build_character_evidence_text(
    *,
    script_id: str,
    canonical_name: str,
    aliases: list[str],
    engine: Engine,
    max_chars: int,
) -> str:
    names = [canonical_name, *aliases]
    conditions: list[str] = []
    params: dict[str, object] = {"sid": script_id}
    for i, name in enumerate(names[:8]):
        key = f"n{i}"
        params[key] = f"%{name}%"
        conditions.append(f"text ILIKE :{key}")
    if not conditions:
        return ""
    where_sql = " OR ".join(conditions)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT scene_label, text
                FROM scriptlens.scenes
                WHERE script_id = :sid AND ({where_sql})
                ORDER BY episode_no NULLS LAST, scene_no, start_line
                LIMIT 20
                """
            ),
            params,
        ).mappings().all()
    if not rows:
        return ""
    snippets: list[str] = []
    for row in rows:
        label = str(row.get("scene_label") or "").strip()
        raw = str(row.get("text") or "").strip()
        if not raw:
            continue
        snippet = raw[:220] + ("…" if len(raw) > 220 else "")
        snippets.append(f"[{label}] {snippet}" if label else snippet)
    text_blob = "\n".join(snippets).strip()
    if len(text_blob) > max_chars:
        text_blob = text_blob[: max_chars - 1] + "…"
    return text_blob


def load_character_context(target_id: str, *, engine: Engine = default_engine, max_chars: int = 2600) -> CharacterContext | None:
    row = _load_character_row_by_id(target_id, engine=engine)
    if row is None:
        parsed = _parse_character_target(target_id)
        if parsed is None:
            return None
        script_ref, idx = parsed
        row = _load_character_row_by_index(script_ref, idx, engine=engine)
    if row is None:
        return None

    aliases_raw = row.get("aliases")
    aliases: list[str] = []
    if isinstance(aliases_raw, list):
        aliases = [str(x).strip() for x in aliases_raw if str(x).strip()]
    elif isinstance(aliases_raw, str) and aliases_raw.strip():
        aliases = [aliases_raw.strip()]

    canonical_name = str(row.get("canonical_name") or "").strip()
    evidence_text = _build_character_evidence_text(
        script_id=str(row["script_id"]),
        canonical_name=canonical_name,
        aliases=aliases,
        engine=engine,
        max_chars=max_chars,
    )
    character_text = (
        f"canonical_name: {canonical_name}\n"
        f"aliases: {', '.join(aliases) if aliases else 'none'}\n"
        f"current_role: {str(row.get('role') or 'unknown')}\n\n"
        f"evidence:\n{evidence_text}"
    )
    return CharacterContext(
        character_id=str(row["id"]),
        script_id=str(row["script_id"]),
        canonical_name=canonical_name,
        aliases=aliases,
        role=str(row.get("role") or "") or None,
        character_text=character_text.strip(),
    )


def _parse_relationship_target(target_id: str) -> tuple[str, int] | None:
    if "::rel::" not in target_id:
        return None
    left, _, right = target_id.partition("::rel::")
    try:
        idx = int(right.strip())
    except ValueError:
        return None
    return left.strip(), idx


def _load_relationship_row_by_id(target_id: str, *, engine: Engine) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT r.id::text AS id, r.script_id::text AS script_id,
                       r.src_char_id::text AS src_char_id, r.dst_char_id::text AS dst_char_id,
                       r.relationship_type, r.polarity, r.dynamic_arc, r.triangle,
                       s.canonical_name AS src_name, d.canonical_name AS dst_name
                FROM scriptlens.character_relationships r
                JOIN scriptlens.character_entities s ON s.id = r.src_char_id
                JOIN scriptlens.character_entities d ON d.id = r.dst_char_id
                WHERE r.id::text = :rid
                LIMIT 1
                """
            ),
            {"rid": target_id},
        ).mappings().first()
    return dict(row) if row else None


def _load_relationship_row_by_index(script_ref: str, idx: int, *, engine: Engine) -> dict | None:
    script_id = resolve_script_id(script_ref, engine=engine)
    if script_id is None:
        return None
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT r.id::text AS id, r.script_id::text AS script_id,
                       r.src_char_id::text AS src_char_id, r.dst_char_id::text AS dst_char_id,
                       r.relationship_type, r.polarity, r.dynamic_arc, r.triangle,
                       s.canonical_name AS src_name, d.canonical_name AS dst_name
                FROM scriptlens.character_relationships r
                JOIN scriptlens.character_entities s ON s.id = r.src_char_id
                JOIN scriptlens.character_entities d ON d.id = r.dst_char_id
                WHERE r.script_id = :sid
                ORDER BY r.created_at, r.id
                OFFSET :offset LIMIT 1
                """
            ),
            {"sid": script_id, "offset": max(idx - 1, 0)},
        ).mappings().first()
    return dict(row) if row else None


def _build_relationship_evidence_text(
    *,
    script_id: str,
    src_name: str,
    dst_name: str,
    engine: Engine,
    max_chars: int,
) -> str:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT summary
                FROM scriptlens.plot_units
                WHERE script_id = :sid
                  AND summary ILIKE :src_like
                  AND summary ILIKE :dst_like
                ORDER BY idx
                LIMIT 20
                """
            ),
            {"sid": script_id, "src_like": f"%{src_name}%", "dst_like": f"%{dst_name}%"},
        ).mappings().all()
    if not rows:
        return ""
    snippets = [str(r.get("summary") or "").strip() for r in rows if str(r.get("summary") or "").strip()]
    text_blob = "\n".join(snippets).strip()
    if len(text_blob) > max_chars:
        text_blob = text_blob[: max_chars - 1] + "…"
    return text_blob


def load_relationship_context(target_id: str, *, engine: Engine = default_engine, max_chars: int = 2600) -> RelationshipContext | None:
    row = _load_relationship_row_by_id(target_id, engine=engine)
    if row is None:
        parsed = _parse_relationship_target(target_id)
        if parsed is None:
            return None
        script_ref, idx = parsed
        row = _load_relationship_row_by_index(script_ref, idx, engine=engine)
    if row is None:
        return None

    src_name = str(row.get("src_name") or "").strip()
    dst_name = str(row.get("dst_name") or "").strip()
    evidence_text = _build_relationship_evidence_text(
        script_id=str(row["script_id"]),
        src_name=src_name,
        dst_name=dst_name,
        engine=engine,
        max_chars=max_chars,
    )
    relationship_text = (
        f"pair: {src_name} -> {dst_name}\n"
        f"current_relationship_type: {str(row.get('relationship_type') or 'none')}\n"
        f"current_polarity: {str(row.get('polarity') or 'none')}\n"
        f"current_dynamic_arc: {str(row.get('dynamic_arc') or 'none')}\n"
        f"current_triangle: {str(row.get('triangle') or 'none')}\n\n"
        f"evidence:\n{evidence_text}"
    )
    return RelationshipContext(
        relationship_id=str(row["id"]),
        script_id=str(row["script_id"]),
        src_char_id=str(row["src_char_id"]),
        dst_char_id=str(row["dst_char_id"]),
        src_name=src_name,
        dst_name=dst_name,
        relationship_text=relationship_text.strip(),
    )

