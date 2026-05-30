"""节奏曲线 v4 — 情感命运曲线（emotion arc）。

完整契约 / 计算口径 / 默认参数：docs/2026-05-30-pacing-curve-v4.md

数据来源：reward_events + beat_sheet + scenes，零 plot_unit / 额外 LLM。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.engine import Engine

from schemas.script import BeatSheet
from service.script_tools.reward_extractor import RewardEvent
from service.script_tools.scene_repo import Scene, get_all_scenes
from utils.database import engine as default_engine


# ---------------------------------------------------------------------------
# 词典 & 常量
# ---------------------------------------------------------------------------

_POSITIVE_TERMS: tuple[str, ...] = (
    "笑", "成功", "团圆", "原谅", "相认", "保护", "幸福",
    "逆袭", "真相大白", "反击", "胜利", "拥抱", "甜蜜",
)
_NEGATIVE_TERMS: tuple[str, ...] = (
    "哭", "死", "怒", "恨", "背叛", "羞辱", "痛苦",
    "绝望", "威胁", "下跪", "崩溃", "失去",
)

# event_type → (基础符号, 强度)；reversal 取基线符号
_REWARD_SENTIMENT: Dict[str, float] = {
    "face_slap": 0.85,
    "revenge": 0.9,
    "underdog_rise": 0.9,
    "romantic_progress": 0.75,
    "humiliate_villain": 0.7,
    "scheme_exposed": 0.55,
    "identity_reveal": 0.4,
    # reversal 单独处理（基线符号 × 0.65）
}

# 节拍锚点：5 个关键 BeatType（与 BeatNode.type 对齐）
_KEY_BEAT_TYPES = frozenset({"opening", "inciting", "midpoint", "climax", "closing"})
_BEAT_LABEL: Dict[str, str] = {
    "opening": "开场",
    "inciting": "激励",
    "midpoint": "中点",
    "climax": "高潮",
    "closing": "收束",
    "reward_spike": "强反转",
}

# 强反转散点：event_type ∈ 这个集合的高置信 reward 进 reward_spike pin
_REWARD_SPIKE_TYPES = frozenset({"reversal", "face_slap", "revenge", "underdog_rise"})

_DEAD_ZONE_SENTIMENT_THRESHOLD = 0.15
_DEAD_ZONE_MIN_SPAN = 6
_REWARD_SPIKE_TOP_N = 8


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class PacingPoint:
    progress: float
    episode_no: Optional[int]
    scene_no: str
    scene_id: str
    sentiment: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "progress": round(self.progress, 4),
            "episode_no": self.episode_no,
            "scene_no": self.scene_no,
            "scene_id": self.scene_id,
            "sentiment": round(self.sentiment, 3),
        }


@dataclass
class PacingBeat:
    progress: float
    beat_type: str
    label: str
    summary: str
    scene_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "progress": round(self.progress, 4),
            "beat_type": self.beat_type,
            "label": self.label,
            "summary": self.summary,
            "scene_id": self.scene_id,
        }


@dataclass
class PacingDeadZone:
    start_progress: float
    end_progress: float
    span_scenes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_progress": round(self.start_progress, 4),
            "end_progress": round(self.end_progress, 4),
            "span_scenes": self.span_scenes,
        }


@dataclass
class PacingCurveResult:
    shape: str = "complex"
    shape_label: str = "复杂"
    climax_progress: float = 0.0
    points: List[PacingPoint] = field(default_factory=list)
    beats: List[PacingBeat] = field(default_factory=list)
    dead_zones: List[PacingDeadZone] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "shape_label": self.shape_label,
            "climax_progress": round(self.climax_progress, 4),
            "points": [p.to_dict() for p in self.points],
            "beats": [b.to_dict() for b in self.beats],
            "dead_zones": [d.to_dict() for d in self.dead_zones],
        }


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

def aggregate_pacing_curve(
    *,
    script_id: str,
    reward_events: List[RewardEvent],
    beat_sheet: Optional[BeatSheet] = None,
    engine: Engine = default_engine,
) -> Optional[dict[str, Any]]:
    scenes = get_all_scenes(script_id=script_id, engine=engine)
    if not scenes:
        return None

    rewards_by_scene = _index_rewards_by_scene(reward_events or [])
    raw = [_scene_sentiment(s, rewards_by_scene.get(s.id, [])) for s in scenes]
    smoothed = _smooth(raw)

    total = len(scenes)
    points = [
        PacingPoint(
            progress=(idx / max(1, total - 1)) if total > 1 else 0.0,
            episode_no=scene.episode_no,
            scene_no=scene.scene_no,
            scene_id=scene.id,
            sentiment=smoothed[idx],
        )
        for idx, scene in enumerate(scenes)
    ]

    beats = _build_beats(
        scenes=scenes,
        points=points,
        beat_sheet=beat_sheet,
        rewards_by_scene=rewards_by_scene,
    )
    dead_zones = _detect_dead_zones(points=points, rewards_by_scene=rewards_by_scene)
    shape, shape_label = _classify_shape(points)
    climax_progress = _argmax_progress(points)

    result = PacingCurveResult(
        shape=shape,
        shape_label=shape_label,
        climax_progress=climax_progress,
        points=points,
        beats=beats,
        dead_zones=dead_zones,
    )
    return result.to_dict()


# ---------------------------------------------------------------------------
# 内部：sentiment 计算
# ---------------------------------------------------------------------------

def _index_rewards_by_scene(events: List[RewardEvent]) -> Dict[str, List[RewardEvent]]:
    idx: Dict[str, List[RewardEvent]] = {}
    for ev in events:
        if not ev.scene_id:
            continue
        idx.setdefault(ev.scene_id, []).append(ev)
    return idx


def _baseline_sentiment(text: str) -> float:
    if not text:
        return 0.0
    pos = sum(text.count(t) for t in _POSITIVE_TERMS)
    neg = sum(text.count(t) for t in _NEGATIVE_TERMS)
    total = pos + neg
    if total == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / total))


def _reward_override(events: List[RewardEvent], baseline: float) -> Optional[float]:
    """命中 reward 时返回覆盖值；仅 confidence=high 参与。"""
    best_abs = 0.0
    best_val: Optional[float] = None
    for ev in events:
        if getattr(ev, "confidence", "high") != "high":
            continue
        etype = ev.event_type
        if etype == "reversal":
            sign = -1.0 if baseline < 0 else 1.0
            val = sign * 0.65
        elif etype in _REWARD_SENTIMENT:
            val = _REWARD_SENTIMENT[etype]
        else:
            continue
        if abs(val) > best_abs:
            best_abs = abs(val)
            best_val = val
    return best_val


def _scene_sentiment(scene: Scene, events: List[RewardEvent]) -> float:
    baseline = _baseline_sentiment(scene.text or "")
    override = _reward_override(events, baseline)
    return override if override is not None else baseline


def _smooth(raw: List[float]) -> List[float]:
    """3 窗口加权平均 → EMA(α=0.4)。"""
    n = len(raw)
    if n == 0:
        return []
    weighted = [0.0] * n
    for i in range(n):
        if i == 0:
            weighted[i] = 0.75 * raw[i] + 0.25 * raw[i + 1] if n > 1 else raw[i]
        elif i == n - 1:
            weighted[i] = 0.75 * raw[i] + 0.25 * raw[i - 1]
        else:
            weighted[i] = 0.5 * raw[i] + 0.25 * raw[i - 1] + 0.25 * raw[i + 1]
    ema: List[float] = []
    alpha = 0.4
    prev = weighted[0]
    for v in weighted:
        prev = alpha * v + (1 - alpha) * prev
        ema.append(round(max(-1.0, min(1.0, prev)), 4))
    return ema


# ---------------------------------------------------------------------------
# 内部：beat 锚点
# ---------------------------------------------------------------------------

def _build_beats(
    *,
    scenes: List[Scene],
    points: List[PacingPoint],
    beat_sheet: Optional[BeatSheet],
    rewards_by_scene: Dict[str, List[RewardEvent]],
) -> List[PacingBeat]:
    scene_index = {s.id: i for i, s in enumerate(scenes)}
    total = len(scenes)
    beats: Dict[str, PacingBeat] = {}

    if beat_sheet:
        for act in beat_sheet.acts or []:
            for node in act.beats or []:
                if node.type not in _KEY_BEAT_TYPES:
                    continue
                if node.anchor_scene_id not in scene_index:
                    continue
                idx = scene_index[node.anchor_scene_id]
                progress = (idx / max(1, total - 1)) if total > 1 else 0.0
                beats[node.anchor_scene_id] = PacingBeat(
                    progress=progress,
                    beat_type=node.type,
                    label=_BEAT_LABEL.get(node.type, node.type),
                    summary=(node.summary or "")[:60],
                    scene_id=node.anchor_scene_id,
                )

    spike_candidates: List[tuple[float, RewardEvent, int]] = []
    for sid, evs in rewards_by_scene.items():
        if sid not in scene_index or sid in beats:
            continue
        for ev in evs:
            if getattr(ev, "confidence", "high") != "high":
                continue
            if ev.event_type not in _REWARD_SPIKE_TYPES:
                continue
            idx = scene_index[sid]
            spike_candidates.append((abs(points[idx].sentiment), ev, idx))

    spike_candidates.sort(key=lambda x: x[0], reverse=True)
    for _, ev, idx in spike_candidates[:_REWARD_SPIKE_TOP_N]:
        sid = ev.scene_id
        if sid in beats:
            continue
        progress = (idx / max(1, total - 1)) if total > 1 else 0.0
        summary_text = ev.evidence if hasattr(ev, "evidence") else (ev.claim or "")
        beats[sid] = PacingBeat(
            progress=progress,
            beat_type="reward_spike",
            label=_BEAT_LABEL["reward_spike"],
            summary=(summary_text or "")[:60],
            scene_id=sid,
        )

    return sorted(beats.values(), key=lambda b: b.progress)


# ---------------------------------------------------------------------------
# 内部：dead zone
# ---------------------------------------------------------------------------

def _detect_dead_zones(
    *,
    points: List[PacingPoint],
    rewards_by_scene: Dict[str, List[RewardEvent]],
) -> List[PacingDeadZone]:
    zones: List[PacingDeadZone] = []
    n = len(points)
    if n < _DEAD_ZONE_MIN_SPAN:
        return zones

    run_start = -1
    for i, p in enumerate(points):
        flat = abs(p.sentiment) < _DEAD_ZONE_SENTIMENT_THRESHOLD
        no_reward = p.scene_id not in rewards_by_scene
        if flat and no_reward:
            if run_start < 0:
                run_start = i
        else:
            if run_start >= 0 and i - run_start >= _DEAD_ZONE_MIN_SPAN:
                zones.append(
                    PacingDeadZone(
                        start_progress=points[run_start].progress,
                        end_progress=points[i - 1].progress,
                        span_scenes=i - run_start,
                    )
                )
            run_start = -1
    if run_start >= 0 and n - run_start >= _DEAD_ZONE_MIN_SPAN:
        zones.append(
            PacingDeadZone(
                start_progress=points[run_start].progress,
                end_progress=points[n - 1].progress,
                span_scenes=n - run_start,
            )
        )
    return zones


# ---------------------------------------------------------------------------
# 内部：shape 识别
# ---------------------------------------------------------------------------

def _classify_shape(points: List[PacingPoint]) -> tuple[str, str]:
    if len(points) < 6:
        return "complex", "复杂"

    values = [p.sentiment for p in points]
    n = len(values)
    third = max(1, n // 3)
    start_mean = sum(values[:third]) / third
    mid_mean = sum(values[third : 2 * third]) / max(1, third)
    end_mean = sum(values[2 * third :]) / max(1, n - 2 * third)
    spread = max(values) - min(values)

    if spread < 0.3:
        return "flat", "平铺型"
    if start_mean <= -0.2 and end_mean >= 0.4:
        return "rags_to_riches", "逆袭型"
    if start_mean >= 0.3 and end_mean <= -0.3:
        return "tragedy", "悲剧型"
    if start_mean < 0 and mid_mean < 0 and end_mean > 0.2:
        return "man_in_hole", "绝处逢生"
    if start_mean < 0 and mid_mean > 0.2 and end_mean < 0:
        return "icarus", "巅峰跌落"
    if start_mean > 0.1 and mid_mean < 0 and end_mean > 0.2:
        return "cinderella", "灰姑娘"
    if start_mean > 0.1 and mid_mean > 0.1 and end_mean < -0.1:
        return "oedipus", "渐入低谷"
    return "complex", "复杂双弧"


def _argmax_progress(points: List[PacingPoint]) -> float:
    if not points:
        return 0.0
    best_idx = 0
    best_val = points[0].sentiment
    for i, p in enumerate(points):
        if p.sentiment > best_val:
            best_val = p.sentiment
            best_idx = i
    return points[best_idx].progress
