"""无 LLM 节奏聚合：每集事件密度 + 情感弧。

该模块只做可解释的统计，不做主观评分。它给前端故事页画 pacing curve，
不替代 5 维 `pacing` 评分。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from sqlalchemy.engine import Engine

from service.script_tools.reward_extractor import RewardEvent
from service.script_tools.scene_repo import get_all_scenes
from utils.database import engine as default_engine


@dataclass
class PacingPoint:
    episode_no: int
    scene_count: int
    event_count: int
    hooks: int
    twists: int
    reward_events: int
    sentiment: float

    def to_dict(self) -> dict:
        return {
            "episode_no": self.episode_no,
            "scene_count": self.scene_count,
            "event_count": self.event_count,
            "hooks": self.hooks,
            "twists": self.twists,
            "reward_events": self.reward_events,
            "sentiment": self.sentiment,
        }


_HOOK_TERMS = (
    "离婚",
    "重生",
    "穿越",
    "死亡",
    "绝症",
    "背叛",
    "羞辱",
    "怀孕",
    "替嫁",
    "身份",
    "秘密",
    "真相",
)

_TWIST_EVENT_TYPES = {
    "reversal",
    "identity_reveal",
    "scheme_exposed",
}

_POSITIVE_TERMS = (
    "赢",
    "成功",
    "团圆",
    "原谅",
    "相认",
    "保护",
    "幸福",
    "逆袭",
    "真相大白",
    "反击",
)

_NEGATIVE_TERMS = (
    "哭",
    "死",
    "恨",
    "骗",
    "背叛",
    "羞辱",
    "痛苦",
    "绝望",
    "威胁",
    "下跪",
)


def aggregate_pacing_curve(
    *,
    script_id: str,
    reward_events: List[RewardEvent],
    engine: Engine = default_engine,
) -> List[dict]:
    """按集聚合节奏曲线。

    `reward_events` 已由 reward_extractor 二级判定过滤，适合作为事件密度基础。
    hook 与 sentiment 用规则统计，保证该模块可重复、可解释、无额外 LLM 成本。
    """
    scenes = get_all_scenes(script_id=script_id, engine=engine)
    if not scenes:
        return []

    episode_nos = sorted({s.episode_no for s in scenes if s.episode_no is not None})
    if not episode_nos:
        episode_nos = [1]

    scenes_by_episode: Dict[int, list] = {ep: [] for ep in episode_nos}
    for scene in scenes:
        ep = scene.episode_no if scene.episode_no is not None else episode_nos[0]
        scenes_by_episode.setdefault(ep, []).append(scene)

    reward_by_episode: Dict[int, List[RewardEvent]] = {ep: [] for ep in scenes_by_episode}
    for event in reward_events:
        ep = event.episode_no if event.episode_no is not None else episode_nos[0]
        reward_by_episode.setdefault(ep, []).append(event)

    points: List[PacingPoint] = []
    for ep in sorted(scenes_by_episode):
        ep_scenes = scenes_by_episode[ep]
        ep_events = reward_by_episode.get(ep, [])
        hooks = _count_hooks(ep_scenes)
        twists = sum(1 for ev in ep_events if ev.event_type in _TWIST_EVENT_TYPES)
        reward_count = len(ep_events)
        points.append(
            PacingPoint(
                episode_no=ep,
                scene_count=len(ep_scenes),
                event_count=hooks + twists + reward_count,
                hooks=hooks,
                twists=twists,
                reward_events=reward_count,
                sentiment=_episode_sentiment(ep_scenes),
            )
        )
    return [p.to_dict() for p in points]


def _count_hooks(scenes: list) -> int:
    count = 0
    for scene in scenes[:3]:
        text = scene.text or ""
        if any(term in text for term in _HOOK_TERMS):
            count += 1
    return count


def _episode_sentiment(scenes: list) -> float:
    text = "\n".join((s.text or "") for s in scenes)
    if not text:
        return 0.0
    pos = sum(text.count(term) for term in _POSITIVE_TERMS)
    neg = sum(text.count(term) for term in _NEGATIVE_TERMS)
    total = pos + neg
    if total == 0:
        return 0.0
    score = (pos - neg) / total
    return round(max(-1.0, min(1.0, score)), 3)
