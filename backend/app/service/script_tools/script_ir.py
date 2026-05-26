from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.scene_repo import Scene, get_all_scenes
from utils.database import engine as default_engine


LineKind = Literal["dialogue", "os", "vo", "action", "scene_header", "stage_direction"]

_SCENE_HEADER_RE = re.compile(
    r"^(?:第?\s*\d+\s*场|场\s*\d+|scene\b|int\.|ext\.|内景|外景|日|夜)",
    re.IGNORECASE,
)
_OS_RE = re.compile(r"(?:\bOS\b|（\s*OS\s*）|旁白\s*[：:]|内心独白\s*[：:])", re.IGNORECASE)
_VO_RE = re.compile(r"(?:\bVO\b|V\.O\.|（\s*VO\s*）|画外音\s*[：:])", re.IGNORECASE)
_DIALOGUE_RE = re.compile(
    r"^[\[\(（【]?\s*(?P<char>[\u4e00-\u9fffA-Za-z0-9_·]{1,24})\s*[\]\)）】]?\s*[：:]\s*(?P<text>.+)$"
)


@dataclass
class IRLine:
    idx: int
    kind: LineKind
    character: Optional[str]
    text: str
    abs_line: Optional[int]


@dataclass
class IRScene:
    scene_id: str
    episode_no: Optional[int]
    scene_no: str
    scene_label: str
    characters: list[str]
    lines: list[IRLine]
    start_line: Optional[int]
    end_line: Optional[int]


@dataclass
class IREpisode:
    episode_no: int
    scenes: list[IRScene]


@dataclass
class ScriptIR:
    script_id: str
    title: Optional[str]
    episodes: list[IREpisode]


def classify_line(raw_line: str) -> tuple[LineKind, Optional[str], str]:
    line = (raw_line or "").strip()
    if not line:
        return "stage_direction", None, ""

    if _SCENE_HEADER_RE.match(line):
        return "scene_header", None, line

    if _OS_RE.search(line):
        # Keep text after separator as the actual content.
        parts = re.split(r"[：:]", line, maxsplit=1)
        text_body = parts[1].strip() if len(parts) == 2 else line
        return "os", None, text_body

    if _VO_RE.search(line):
        parts = re.split(r"[：:]", line, maxsplit=1)
        text_body = parts[1].strip() if len(parts) == 2 else line
        return "vo", None, text_body

    m = _DIALOGUE_RE.match(line)
    if m:
        character = m.group("char").strip()
        text_body = m.group("text").strip()
        return "dialogue", character, text_body

    if (line.startswith(("(", "（", "[", "【")) and line.endswith((")", "）", "]", "】"))) or line.startswith(
        ("动作", "镜头", "切", "转", "灯光")
    ):
        return "action", None, line

    return "stage_direction", None, line


def _to_ir_scene(scene: Scene) -> IRScene:
    lines: list[IRLine] = []
    raw_lines = (scene.text or "").splitlines()
    for idx, raw in enumerate(raw_lines, start=1):
        kind, character, body = classify_line(raw)
        abs_line = scene.start_line + idx - 1 if scene.start_line is not None else None
        lines.append(
            IRLine(
                idx=idx,
                kind=kind,
                character=character,
                text=body,
                abs_line=abs_line,
            )
        )
    return IRScene(
        scene_id=scene.id,
        episode_no=scene.episode_no,
        scene_no=scene.scene_no,
        scene_label=scene.scene_label,
        characters=list(scene.characters or []),
        lines=lines,
        start_line=scene.start_line,
        end_line=scene.end_line,
    )


def _load_script_title(script_id: str, *, engine: Engine) -> Optional[str]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT title FROM scriptlens.scripts WHERE id = :sid"),
            {"sid": script_id},
        ).mappings().first()
    if not row:
        return None
    return row.get("title")


def build_script_ir(
    script_id: str,
    *,
    limit: int = 5000,
    engine: Engine = default_engine,
) -> ScriptIR:
    scenes = get_all_scenes(script_id=script_id, limit=limit, engine=engine)
    episodes_map: dict[int, list[IRScene]] = {}
    for scene in scenes:
        ep_no = scene.episode_no if scene.episode_no is not None else 0
        episodes_map.setdefault(ep_no, []).append(_to_ir_scene(scene))

    episodes: list[IREpisode] = []
    for ep_no in sorted(episodes_map.keys()):
        episode_scenes = episodes_map[ep_no]
        episodes.append(IREpisode(episode_no=ep_no, scenes=episode_scenes))

    return ScriptIR(
        script_id=script_id,
        title=_load_script_title(script_id, engine=engine),
        episodes=episodes,
    )
