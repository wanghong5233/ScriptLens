from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.extractor_common import load_plot_unit_context, resolve_script_id
from utils.database import engine as default_engine


@dataclass
class RelationshipCandidate:
    src_char_id: str
    dst_char_id: str
    cooccurrence: int
    evidence_plot_unit_ids: list[str]


def _contains_name(text_blob: str, name: str) -> bool:
    return bool(name and name in text_blob)


def _collect_plot_unit_character_hits(
    *,
    script_id: str,
    characters: list[dict[str, Any]],
    engine: Engine,
) -> dict[str, set[str]]:
    with engine.connect() as conn:
        units = conn.execute(
            text(
                """
                SELECT id::text AS id
                FROM scriptlens.plot_units
                WHERE script_id = :sid
                ORDER BY idx
                LIMIT 800
                """
            ),
            {"sid": script_id},
        ).mappings().all()

    unit_hits: dict[str, set[str]] = {}
    for row in units:
        plot_unit_id = str(row["id"])
        context = load_plot_unit_context(plot_unit_id, engine=engine)
        if context is None:
            continue
        text_blob = f"{context.summary}\n{context.full_text}"
        hit_ids: set[str] = set()
        for ch in characters:
            char_id = str(ch["id"])
            names = [str(ch.get("canonical_name") or "").strip(), *[str(x).strip() for x in ch.get("aliases", []) if str(x).strip()]]
            if any(_contains_name(text_blob, n) for n in names if n):
                hit_ids.add(char_id)
        if len(hit_ids) >= 2:
            unit_hits[plot_unit_id] = hit_ids
    return unit_hits


def build_relationship_candidates(
    unit_hits: dict[str, set[str]],
    *,
    min_cooccurrence: int = 3,
    top_k: int = 15,
) -> list[RelationshipCandidate]:
    pair_to_units: dict[tuple[str, str], set[str]] = {}
    for plot_unit_id, char_ids in unit_hits.items():
        sorted_ids = sorted(char_ids)
        for left, right in combinations(sorted_ids, 2):
            key = (left, right)
            pair_to_units.setdefault(key, set()).add(plot_unit_id)

    scored: list[RelationshipCandidate] = []
    for (src_char_id, dst_char_id), plot_unit_ids in pair_to_units.items():
        if len(plot_unit_ids) < min_cooccurrence:
            continue
        scored.append(
            RelationshipCandidate(
                src_char_id=src_char_id,
                dst_char_id=dst_char_id,
                cooccurrence=len(plot_unit_ids),
                evidence_plot_unit_ids=sorted(plot_unit_ids),
            )
        )
    scored.sort(key=lambda c: (-c.cooccurrence, c.src_char_id, c.dst_char_id))
    return scored[:top_k]


def _upsert_candidates(
    *,
    script_id: str,
    tag_set_ver: str,
    candidates: list[RelationshipCandidate],
    engine: Engine,
) -> None:
    with engine.begin() as conn:
        for c in candidates:
            evidence = {
                "cooccurrence": c.cooccurrence,
                "evidence_plot_unit_ids": c.evidence_plot_unit_ids,
            }
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.character_relationships
                        (id, script_id, src_char_id, dst_char_id, relationship_type, polarity,
                         dynamic_arc, triangle, evidence, tag_set_ver, source, created_at)
                    VALUES
                        (:id, :script_id, :src_char_id, :dst_char_id, NULL, NULL,
                         NULL, NULL, CAST(:evidence AS jsonb), :tag_set_ver, 'llm', NOW())
                    ON CONFLICT (src_char_id, dst_char_id, tag_set_ver)
                    DO UPDATE SET
                        evidence = scriptlens.character_relationships.evidence || EXCLUDED.evidence,
                        source = EXCLUDED.source
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "script_id": script_id,
                    "src_char_id": c.src_char_id,
                    "dst_char_id": c.dst_char_id,
                    "evidence": json.dumps(evidence, ensure_ascii=False),
                    "tag_set_ver": tag_set_ver,
                },
            )


def ensure_relationship_candidates(
    script_ref: str,
    *,
    tag_set_ver: str = "script",
    min_cooccurrence: int = 3,
    top_k: int = 15,
    persist: bool = True,
    engine: Engine = default_engine,
) -> list[RelationshipCandidate]:
    script_id = resolve_script_id(script_ref, engine=engine) or script_ref
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id, canonical_name, aliases
                FROM scriptlens.character_entities
                WHERE script_id = :sid
                ORDER BY created_at, canonical_name
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    characters: list[dict[str, Any]] = []
    for row in rows:
        aliases = row.get("aliases")
        parsed_aliases = aliases if isinstance(aliases, list) else []
        characters.append(
            {
                "id": str(row["id"]),
                "canonical_name": str(row.get("canonical_name") or ""),
                "aliases": [str(x) for x in parsed_aliases],
            }
        )
    if len(characters) < 2:
        return []

    unit_hits = _collect_plot_unit_character_hits(script_id=script_id, characters=characters, engine=engine)
    candidates = build_relationship_candidates(
        unit_hits,
        min_cooccurrence=min_cooccurrence,
        top_k=top_k,
    )
    if persist and candidates:
        _upsert_candidates(script_id=script_id, tag_set_ver=tag_set_ver, candidates=candidates, engine=engine)
    return candidates
