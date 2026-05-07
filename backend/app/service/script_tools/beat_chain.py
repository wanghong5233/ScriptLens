"""故事节拍抽取：三幕骨架 + 短剧关键节拍。

短剧不强套 90 分钟电影的 15 节拍；这里采用三幕骨架，并把 opening /
inciting / midpoint / climax / closing / twist / reward 作为可点击锚点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.engine import Engine

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget
from service.script_tools.reward_extractor import RewardEvent
from service.script_tools.scene_repo import Scene, get_all_scenes
from utils.database import engine as default_engine


BeatType = str


@dataclass
class BeatNode:
    type: BeatType
    summary: str
    anchor_scene_id: str

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "summary": self.summary,
            "anchor_scene_id": self.anchor_scene_id,
        }


@dataclass
class BeatAct:
    act: int
    title: str
    scene_range: List[str] = field(default_factory=list)
    beats: List[BeatNode] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "act": self.act,
            "title": self.title,
            "scene_range": self.scene_range,
            "beats": [b.to_dict() for b in self.beats],
        }


@dataclass
class BeatSheet:
    acts: List[BeatAct] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"acts": [a.to_dict() for a in self.acts]}


_ALLOWED_BEATS = {"opening", "inciting", "midpoint", "climax", "closing", "twist", "reward"}

_SYSTEM_PROMPT = """你是中文短剧剧本统筹，负责把长剧本整理成「三幕故事骨架」。

不要写电影理论术语，不要写技术词。输出要帮助选品/编剧/审核快速判断重点该看哪里。
"""

_PROMPT = """下面是从剧本抽样出的关键场景。请整理成三幕骨架，并给出每幕关键节拍。

【场景样本】
{scenes_block}

【输出 JSON】
{{
  "acts": [
    {{
      "act": 1,
      "title": "开局",
      "scene_range": ["<起始scene_id>", "<结束scene_id>"],
      "beats": [
        {{"type": "opening", "summary": "≤50字", "anchor_scene_id": "<scene_id>"}}
      ]
    }}
  ]
}}

规则：
1. acts 必须恰好 3 幕：1=开局，2=发展，3=收束。
2. type 只能是 opening/inciting/midpoint/climax/closing/twist/reward。
3. anchor_scene_id 必须来自上方样本。
4. summary 概括整场戏的故事功能，不要摘一句台词。
5. 每幕 1-4 个 beats，总 beats 5-9 个。
"""


async def extract_beat_sheet(
    *,
    script_id: str,
    reward_events: Optional[List[RewardEvent]] = None,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> BeatSheet:
    caller = caller or LlmCaller()
    scenes = get_all_scenes(script_id=script_id, engine=engine)
    if not scenes:
        raise ValueError(f"script_id={script_id} 没有可分析的场景")

    sampled = _sample_scenes(scenes, reward_events or [])
    allowed_ids = {s.id for s in sampled}
    prompt = _PROMPT.format(scenes_block=_scenes_block(sampled))
    resp = await caller.call_json(
        prompt=prompt,
        tier=ModelTier.PRIMARY,
        system_message=_SYSTEM_PROMPT,
        temperature=0.2,
        max_tokens=TokenBudget.BEAT_SHEET,
    )
    parsed = resp.parsed if isinstance(resp.parsed, dict) else None
    if parsed is None:
        raise ScoreLLMError("beat_chain: LLM 返回非 JSON object")

    raw_acts = parsed.get("acts")
    if not isinstance(raw_acts, list):
        raise ScoreLLMError("beat_chain: missing acts list")

    acts: List[BeatAct] = []
    for raw in raw_acts:
        if not isinstance(raw, dict):
            continue
        act_no = raw.get("act")
        if act_no not in (1, 2, 3):
            continue
        acts.append(
            BeatAct(
                act=act_no,
                title=_act_title(act_no, str(raw.get("title") or "")),
                scene_range=_scene_range(raw.get("scene_range"), allowed_ids),
                beats=_beats(raw.get("beats"), allowed_ids),
            )
        )

    by_act = {a.act: a for a in acts}
    if set(by_act) != {1, 2, 3}:
        raise ScoreLLMError("beat_chain: acts 必须覆盖 1/2/3")
    return BeatSheet(acts=[by_act[1], by_act[2], by_act[3]])


def _sample_scenes(scenes: List[Scene], reward_events: List[RewardEvent], max_count: int = 26) -> List[Scene]:
    """三段采样 + reward 场补充，控制 prompt 同时覆盖全剧走势。"""
    if len(scenes) <= max_count:
        return scenes

    selected: dict[str, Scene] = {}
    spans = (scenes[:8], _middle(scenes, 8), scenes[-8:])
    for span in spans:
        for scene in span:
            selected[scene.id] = scene

    by_id = {s.id: s for s in scenes}
    for event in reward_events[:8]:
        scene = by_id.get(event.scene_id)
        if scene is not None:
            selected[scene.id] = scene
        if len(selected) >= max_count:
            break

    return sorted(
        selected.values(),
        key=lambda s: (s.episode_no if s.episode_no is not None else 9999, s.scene_no, s.start_line or 0),
    )[:max_count]


def _middle(scenes: List[Scene], count: int) -> List[Scene]:
    start = max(0, (len(scenes) // 2) - (count // 2))
    return scenes[start : start + count]


def _scenes_block(scenes: List[Scene]) -> str:
    blocks = []
    for scene in scenes:
        text = scene.text or ""
        if len(text) > 900:
            text = text[:900] + "..."
        blocks.append(
            f"[scene_id={scene.id}] [第{scene.episode_no or '?'}集] "
            f"[{scene.scene_no}] [{scene.scene_label}] [人物:{','.join(scene.characters[:6])}]\n{text}"
        )
    return "\n\n---\n\n".join(blocks)


def _act_title(act_no: int, raw: str) -> str:
    defaults = {1: "开局", 2: "发展", 3: "收束"}
    title = raw.strip()[:12]
    return title or defaults[act_no]


def _scene_range(raw: object, allowed_ids: set[str]) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for sid in raw[:2]:
        text = str(sid or "").strip()
        if text in allowed_ids:
            out.append(text)
    return out


def _beats(raw: object, allowed_ids: set[str]) -> List[BeatNode]:
    if not isinstance(raw, list):
        return []
    out: List[BeatNode] = []
    for item in raw[:4]:
        if not isinstance(item, dict):
            continue
        beat_type = str(item.get("type") or "").strip()
        anchor = str(item.get("anchor_scene_id") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if beat_type not in _ALLOWED_BEATS or anchor not in allowed_ids or not summary:
            continue
        out.append(BeatNode(type=beat_type, summary=summary[:50], anchor_scene_id=anchor))
    return out
