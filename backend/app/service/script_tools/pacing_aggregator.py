"""集级节奏曲线聚合（v3.5 event-based）。

产品定位
========
- 故事 tab 的「节奏曲线」section：x = 集数 / y = 事件密度 / spike 标红
- 帮助选品 / 编剧 / 剪辑快速定位**关键剧情集**与塌陷段
- 业内对照：抖音文心剧本助手集数密度图谱 / YouTube Studio analytics 时序图

数据来源
========
- `reward_events`（reward_extractor 抽出，v3.5 已稳定 high-confidence）
- `scenes`（含 episode_no / 原文）

**零 plot_unit / tag_pipeline 依赖**——这是 v3.5 重写的关键，
让节奏曲线在去掉整剧打标流水线后仍能稳定输出。

度量解释
========
per episode：
- `scene_count`     该集场数（衡量集时长 / 场切密度）
- `event_count`     hooks + twists + reward_events 三者累加的事件密度（前端 y 轴）
- `hooks`           开场 3 场内命中钩子词的次数（首场抓人指标）
- `twists`          reward.event_type ∈ {reversal, identity_reveal, scheme_exposed} 数
- `reward_events`   该集所有 high-confidence reward 数
- `sentiment`       该集正/负情感词比例 [-1, 1]（粗粒度可解释基线）

为什么用关键词法做 sentiment
===========================
- 短剧场景词汇高度集中、情绪极端（虐 / 复仇 / 救赎 / 爽点）
- BERT/RoBERTa sentiment 在该域反而漂移（被训练在新闻 / 影评上）
- 关键词法**可解释、可重复、零额外 LLM 成本**，作为 v1-mvp 基线足够

后续如需更精确，再换 dim 评分维度的方法。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from sqlalchemy.engine import Engine

from service.script_tools.reward_extractor import RewardEvent
from service.script_tools.scene_repo import get_all_scenes
from utils.database import engine as default_engine


@dataclass
class PacingPoint:
    """单集节奏点。字段与 schemas.script.PacingCurvePoint 1:1。"""

    episode_no: int
    scene_count: int
    event_count: int
    hooks: int
    twists: int
    reward_events: int
    sentiment: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_no": self.episode_no,
            "scene_count": self.scene_count,
            "event_count": self.event_count,
            "hooks": self.hooks,
            "twists": self.twists,
            "reward_events": self.reward_events,
            "sentiment": self.sentiment,
        }


# 钩子词：开场 3 场内出现即视为高强度开场
# 业内对照：阅文短剧《短剧爆款公式》/《抖音短剧爆款选品手册》头部钩子高频词清单
_HOOK_TERMS: tuple[str, ...] = (
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
    "复仇",
    "反派",
    "总裁",
    "豪门",
)

# Twist 事件类型：reward_extractor 输出的 event_type 中视为"反转"的子集
_TWIST_EVENT_TYPES = frozenset(
    {
        "reversal",
        "identity_reveal",
        "scheme_exposed",
    }
)

# 正向情感词
_POSITIVE_TERMS: tuple[str, ...] = (
    "笑",
    "成功",
    "团圆",
    "原谅",
    "相认",
    "保护",
    "幸福",
    "逆袭",
    "真相大白",
    "反击",
    "胜利",
    "拥抱",
    "甜蜜",
)

# 负向情感词
_NEGATIVE_TERMS: tuple[str, ...] = (
    "哭",
    "死",
    "怒",
    "恨",
    "背叛",
    "羞辱",
    "痛苦",
    "绝望",
    "威胁",
    "下跪",
    "崩溃",
    "失去",
)


def aggregate_pacing_curve(
    *,
    script_id: str,
    reward_events: List[RewardEvent],
    engine: Engine = default_engine,
) -> List[dict[str, Any]]:
    """按集聚合节奏曲线。

    `reward_events` 已由 reward_extractor 二级判定 + confidence=high 过滤，
    适合直接作为事件密度基础。hook / sentiment 用关键词法统计，保证零 LLM 成本。
    """
    scenes = get_all_scenes(script_id=script_id, engine=engine)
    if not scenes:
        return []

    # 按集分桶；episode_no=None 的散场归入首集，避免丢点
    episode_nos = sorted({s.episode_no for s in scenes if s.episode_no is not None})
    fallback_ep = episode_nos[0] if episode_nos else 1
    scenes_by_episode: Dict[int, list] = {}
    for scene in scenes:
        ep = scene.episode_no if scene.episode_no is not None else fallback_ep
        scenes_by_episode.setdefault(ep, []).append(scene)

    reward_by_episode: Dict[int, List[RewardEvent]] = {}
    for event in reward_events or []:
        ep = event.episode_no if event.episode_no is not None else fallback_ep
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
    """开场 3 场内出现钩子词的场次累加。短剧"首场抓人"指标。"""
    count = 0
    for scene in scenes[:3]:
        text = scene.text or ""
        if any(term in text for term in _HOOK_TERMS):
            count += 1
    return count


def _episode_sentiment(scenes: list) -> float:
    """集内正/负情感词比例。返回 [-1, 1]，全无命中返回 0.0。"""
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
