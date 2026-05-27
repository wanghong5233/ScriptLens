from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.llm_caller import LlmCaller, ModelTier
from service.script_tools.script_ir import build_script_ir
from utils.database import engine as default_engine

_INVALID_NAMES = {
    "人物",
    "角色",
    "场景",
    "地点",
    "时间",
    "旁白",
    "画外音",
    "内心独白",
    "os",
    "vo",
}


@dataclass
class ResolvedCharacterEntity:
    id: str
    script_id: str
    canonical_name: str
    aliases: list[str]
    role: str
    tag_set_ver: str
    source: str = "llm"
    evidence: dict | None = None

    def to_db_payload(self) -> dict:
        payload = asdict(self)
        payload["evidence"] = json.dumps(self.evidence or {}, ensure_ascii=False)
        payload["aliases"] = json.dumps(self.aliases, ensure_ascii=False)
        return payload


def _normalize_name(raw: str) -> str:
    name = (raw or "").strip()
    name = re.sub(r"[（(].*?[)）]", "", name)
    name = name.replace("：", "").replace(":", "").strip()
    return name


def _valid_name(name: str) -> bool:
    if not name:
        return False
    if len(name) > 16:
        return False
    if name.lower() in _INVALID_NAMES:
        return False
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9_·]", name):
        return False
    return True


def _jaro_similarity(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    len1 = len(s1)
    len2 = len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0

    match_distance = max(len1, len2) // 2 - 1
    s1_matches = [False] * len1
    s2_matches = [False] * len2

    matches = 0
    for i in range(len1):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len2)
        for j in range(start, end):
            if s2_matches[j]:
                continue
            if s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    t = 0
    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            t += 1
        k += 1
    transpositions = t / 2
    return (matches / len1 + matches / len2 + (matches - transpositions) / matches) / 3.0


def _jaro_winkler_similarity(s1: str, s2: str, scaling: float = 0.1) -> float:
    jaro = _jaro_similarity(s1, s2)
    prefix_len = 0
    for ch1, ch2 in zip(s1, s2):
        if ch1 == ch2:
            prefix_len += 1
        else:
            break
        if prefix_len == 4:
            break
    return jaro + prefix_len * scaling * (1 - jaro)


def _name_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    contain_score = 0.92 if (a in b or b in a) else 0.0
    return max(_jaro_winkler_similarity(a, b), contain_score)


async def _llm_should_merge(
    *,
    left: str,
    right: str,
    seed: int,
    caller: LlmCaller,
    tag_set_ver: str,
) -> bool:
    if os.getenv("SM_TAGGING_DISABLE_LLM", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    prompt = (
        "你是中文剧本角色名归一助手。判断两个名字是否是同一角色。\n"
        "输出 JSON: {\"merge\": true|false, \"reason\": \"<=20字\"}\n"
        f"名字A: {left}\n名字B: {right}\n"
    )
    try:
        resp = await caller.call_json_deterministic(
            prompt,
            tag_set_ver=tag_set_ver,
            prompt_ver=f"{tag_set_ver}:character_alias_merge:a",
            dim="character_alias_merge",
            seed=seed,
            tier=ModelTier.MINI,
            max_tokens=128,
        )
        parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
        raw = parsed.get("merge")
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"true", "1", "yes", "y"}
    except Exception:
        return False


def _role_of(rank: int, scene_count: int, top_scene_count: int) -> str:
    if rank == 0:
        return "protagonist"
    if rank == 1 and scene_count >= max(2, int(top_scene_count * 0.5)):
        return "antagonist"
    if scene_count >= 2:
        return "supporting"
    return "minor"


def _persist_entities_sync(
    *,
    script_id: str,
    tag_set_ver: str,
    entities: list[ResolvedCharacterEntity],
    engine: Engine,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM scriptlens.character_entities
                WHERE script_id = :sid AND source = 'llm'
                """
            ),
            {"sid": script_id},
        )
        seen_canonical: set[str] = set()
        for entity in entities:
            canonical = (entity.canonical_name or "").strip()
            if not canonical or canonical in seen_canonical:
                continue
            seen_canonical.add(canonical)
            payload = entity.to_db_payload()
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.character_entities
                        (id, script_id, canonical_name, aliases, role, gender, archetype, arc_type,
                         agency_level, tag_set_ver, source, evidence, created_at)
                    VALUES
                        (:id, :script_id, :canonical_name, CAST(:aliases AS jsonb), :role, NULL, NULL, NULL,
                         NULL, :tag_set_ver, :source, CAST(:evidence AS jsonb), NOW())
                    ON CONFLICT (script_id, canonical_name)
                    DO UPDATE SET
                        aliases = EXCLUDED.aliases,
                        role = EXCLUDED.role,
                        tag_set_ver = EXCLUDED.tag_set_ver,
                        source = EXCLUDED.source,
                        evidence = EXCLUDED.evidence
                    """
                ),
                payload,
            )


async def resolve_character_entities(
    script_id: str,
    *,
    tag_set_ver: str = "v0.1.0",
    seed: int = 42,
    caller: Optional[LlmCaller] = None,
    persist: bool = True,
    engine: Engine = default_engine,
) -> list[ResolvedCharacterEntity]:
    """Resolve canonical character entities from ScriptIR and persist to DB."""
    ir = build_script_ir(script_id, engine=engine)
    name_freq: dict[str, int] = {}
    name_scene_hits: dict[str, set[str]] = {}

    for ep in ir.episodes:
        for scene in ep.scenes:
            candidates = list(scene.characters or [])
            for line in scene.lines:
                if line.character:
                    candidates.append(line.character)
            for raw in candidates:
                name = _normalize_name(raw)
                if not _valid_name(name):
                    continue
                name_freq[name] = name_freq.get(name, 0) + 1
                name_scene_hits.setdefault(name, set()).add(scene.scene_id)

    if not name_freq:
        return []

    caller = caller or LlmCaller()
    sorted_names = sorted(name_freq.keys(), key=lambda n: (name_freq[n], len(n)), reverse=True)
    clusters: list[list[str]] = []

    for name in sorted_names:
        merged = False
        for cluster in clusters:
            ref = cluster[0]
            sim = _name_similarity(name, ref)
            if sim >= 0.85:
                cluster.append(name)
                merged = True
                break
            if 0.65 <= sim < 0.85 and (len(cluster) > 5 or name_freq.get(name, 0) + name_freq.get(ref, 0) >= 8):
                if await _llm_should_merge(
                    left=name,
                    right=ref,
                    seed=seed,
                    caller=caller,
                    tag_set_ver=tag_set_ver,
                ):
                    cluster.append(name)
                    merged = True
                    break
        if not merged:
            clusters.append([name])

    scene_counts: list[int] = []
    canon_payloads: list[tuple[str, list[str], int, int]] = []
    for cluster in clusters:
        canonical = sorted(cluster, key=lambda n: (name_freq[n], len(n)), reverse=True)[0]
        aliases = sorted({n for n in cluster if n != canonical})
        mention_count = sum(name_freq.get(n, 0) for n in cluster)
        hit_scenes: set[str] = set()
        for n in cluster:
            hit_scenes.update(name_scene_hits.get(n, set()))
        scene_count = len(hit_scenes)
        scene_counts.append(scene_count)
        canon_payloads.append((canonical, aliases, mention_count, scene_count))

    order = sorted(range(len(canon_payloads)), key=lambda i: canon_payloads[i][3], reverse=True)
    top_scene_count = max(scene_counts) if scene_counts else 1
    entities: list[ResolvedCharacterEntity] = []
    for rank, idx in enumerate(order):
        canonical, aliases, mention_count, scene_count = canon_payloads[idx]
        entities.append(
            ResolvedCharacterEntity(
                id=str(uuid.uuid4()),
                script_id=script_id,
                canonical_name=canonical,
                aliases=aliases,
                role=_role_of(rank, scene_count, top_scene_count),
                tag_set_ver=tag_set_ver,
                source="llm",
                evidence={
                    "mention_count": mention_count,
                    "scene_count": scene_count,
                    "cluster_size": 1 + len(aliases),
                },
            )
        )

    if persist and entities:
        await asyncio.to_thread(
            _persist_entities_sync,
            script_id=script_id,
            tag_set_ver=tag_set_ver,
            entities=entities,
            engine=engine,
        )
    return entities

