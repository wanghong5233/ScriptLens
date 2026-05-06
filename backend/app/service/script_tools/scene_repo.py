"""场景库 helper（DB 只读层）。

把"按 script_id 取场景"的所有变体 SQL 集中到这里，让上层评分工具
（reward_extractor / motivation_chain / risk_screener / dimension_scorer）
不直接写 SQL，只调本模块函数。

不变式：
- 所有查询 limit 默认有界（防止全表 5000 场全部加载到内存）
- 返回 dataclass 而非 dict，让评分工具的类型签名清晰
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


@dataclass
class Scene:
    """场景内存表示。与 DB 字段 1:1。"""

    id: str
    script_id: str
    episode_no: Optional[int]
    scene_no: str
    scene_label: str
    characters: List[str]
    start_line: Optional[int]
    end_line: Optional[int]
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text or "")


def get_scene(*, scene_id: str, engine: Engine = default_engine) -> Optional[Scene]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id, script_id::text AS script_id,
                       episode_no, scene_no, scene_label, characters,
                       start_line, end_line, text
                FROM scriptlens.scenes
                WHERE id = :sid
                """
            ),
            {"sid": scene_id},
        ).mappings().first()
    if not row:
        return None
    return Scene(**dict(row))


def get_all_scenes(
    *,
    script_id: str,
    limit: int = 5000,
    engine: Engine = default_engine,
) -> List[Scene]:
    """全剧所有场景，按 episode_no/scene_no/start_line 排序。"""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id, script_id::text AS script_id,
                       episode_no, scene_no, scene_label, characters,
                       start_line, end_line, text
                FROM scriptlens.scenes
                WHERE script_id = :sid
                ORDER BY episode_no NULLS LAST, scene_no, start_line
                LIMIT :limit
                """
            ),
            {"sid": script_id, "limit": limit},
        ).mappings().all()
    return [Scene(**dict(r)) for r in rows]


def get_first_episode_scenes(
    *,
    script_id: str,
    n_episodes: int = 3,
    engine: Engine = default_engine,
) -> List[Scene]:
    """取前 N 集的所有场景（用于 opening_hook 评分）。

    若剧本无 episode_no（fallback 切分），则按场号顺序取前 6 场作为「开场窗口」。
    """
    all_scenes = get_all_scenes(script_id=script_id, engine=engine)
    if not all_scenes:
        return []
    if all(s.episode_no is None for s in all_scenes):
        return all_scenes[:6]
    eps_seen: set[int] = set()
    out: List[Scene] = []
    for s in all_scenes:
        if s.episode_no is None:
            continue
        if s.episode_no not in eps_seen:
            if len(eps_seen) >= n_episodes:
                break
            eps_seen.add(s.episode_no)
        if s.episode_no in eps_seen:
            out.append(s)
    return out


def locate_scenes_by_keyword(
    *,
    script_id: str,
    keywords: List[str],
    limit: int = 50,
    engine: Engine = default_engine,
) -> List[Scene]:
    """按关键词命中场景（关键词 OR 关系，使用 ILIKE）。

    给 risk_screener / reward_extractor 第一级关键词扫使用。
    """
    if not keywords:
        return []
    # 构造 OR 条件 + 命名参数；避免 SQL 注入
    where_parts: List[str] = []
    params: dict = {"sid": script_id, "limit": limit}
    for i, kw in enumerate(keywords):
        if not kw:
            continue
        params[f"kw_{i}"] = f"%{kw}%"
        where_parts.append(f"text ILIKE :kw_{i}")
    if not where_parts:
        return []
    where_clause = " OR ".join(where_parts)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT id::text AS id, script_id::text AS script_id,
                       episode_no, scene_no, scene_label, characters,
                       start_line, end_line, text
                FROM scriptlens.scenes
                WHERE script_id = :sid AND ({where_clause})
                ORDER BY episode_no NULLS LAST, scene_no
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
    return [Scene(**dict(r)) for r in rows]


def locate_scenes_by_character(
    *,
    script_id: str,
    character: str,
    limit: int = 100,
    engine: Engine = default_engine,
) -> List[Scene]:
    """某角色出现的所有场景（按 characters 数组 + text 双路命中）。"""
    if not character:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id, script_id::text AS script_id,
                       episode_no, scene_no, scene_label, characters,
                       start_line, end_line, text
                FROM scriptlens.scenes
                WHERE script_id = :sid
                  AND (:name = ANY(characters) OR text ILIKE :name_like)
                ORDER BY episode_no NULLS LAST, scene_no
                LIMIT :limit
                """
            ),
            {"sid": script_id, "name": character, "name_like": f"%{character}%", "limit": limit},
        ).mappings().all()
    return [Scene(**dict(r)) for r in rows]


def get_scenes_around(
    *,
    script_id: str,
    target_scene_id: str,
    before: int = 5,
    after: int = 0,
    engine: Engine = default_engine,
) -> List[Scene]:
    """以 target_scene 为锚点取前后场景（motivation_chain 5 场上下文回扫用）。"""
    target = get_scene(scene_id=target_scene_id, engine=engine)
    if not target:
        return []
    all_scenes = get_all_scenes(script_id=script_id, engine=engine)
    idx = next((i for i, s in enumerate(all_scenes) if s.id == target.id), None)
    if idx is None:
        return [target]
    lo = max(0, idx - before)
    hi = min(len(all_scenes), idx + after + 1)
    return all_scenes[lo:hi]


def extract_quote(
    *,
    scene_id: str,
    max_chars: int = 90,
    engine: Engine = default_engine,
) -> Optional[dict]:
    """提取场景里最显著的一段（≤max_chars 字），作为 evidence_refs.quote。

    选择策略：
    1. 优先选第一段对白行（包含「：」或「:」分隔）
    2. 没有对白 → 选首段动作行（▲△ 开头）
    3. 都没有 → 截断 text 前 max_chars 字
    """
    scene = get_scene(scene_id=scene_id, engine=engine)
    if not scene or not scene.text:
        return None
    lines = [ln.strip() for ln in scene.text.split("\n") if ln.strip()]
    quote: Optional[str] = None
    for ln in lines:
        if "：" in ln or ":" in ln:
            quote = ln
            break
    if quote is None:
        for ln in lines:
            if ln.startswith(("▲", "△", "◇", "◆")):
                quote = ln
                break
    if quote is None:
        quote = scene.text
    if len(quote) > max_chars:
        quote = quote[: max_chars - 1] + "…"
    return {
        "quote": quote,
        "scene_id": scene.id,
        "scene_no": scene.scene_no,
        "scene_label": scene.scene_label,
        "start_line": scene.start_line,
        "end_line": scene.end_line,
    }


def get_scene_id_by_no(
    *,
    script_id: str,
    scene_no: str,
    engine: Engine = default_engine,
) -> Optional[str]:
    """已知 scene_no 反查 scene_id（rubric 评分输出 evidence_ref_ids 时用）。"""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id FROM scriptlens.scenes
                WHERE script_id = :sid AND scene_no = :sno
                LIMIT 1
                """
            ),
            {"sid": script_id, "sno": scene_no},
        ).first()
    return row.id if row else None
