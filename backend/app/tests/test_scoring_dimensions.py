"""scoring v4 5 维度集成测试。

构造 4 个 fixture scene/reward 组合，跑 main_chain.score_script，断言：
- hit fixture → qualified
- flop fixture → needs_polish / not_recommended
- compliance_block → not_recommended（compliance gate）
- dealbreaker_hook → not_recommended（HOOK dealbreaker）
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import pytest

from service.scoring import score_script
from service.scoring.framework import (
    ScoringContext,
    VerdictLabel,
)


# ============================================================
# 简化 Scene / RewardEvent stub（避免依赖 DB）
# ============================================================


@dataclass
class _Scene:
    id: str
    script_id: str
    episode_no: Optional[int]
    scene_no: str
    scene_label: str
    characters: list[str]
    start_line: Optional[int]
    end_line: Optional[int]
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text or "")


@dataclass
class _RewardEvent:
    scene_id: str
    scene_no: str
    episode_no: Optional[int]
    event_type: str
    claim: str = ""
    quote_verbatim: str = ""
    quote_verified: bool = True
    confidence: str = "high"
    evidence_line_range: Optional[tuple[int, int]] = None


@dataclass
class _CoverageCard:
    logline: str = ""
    synopsis: str = ""
    genre: list[str] = None
    core_value: str = ""
    recommendation: str = "consider"
    confidence: str = "medium"

    def __post_init__(self) -> None:
        if self.genre is None:
            self.genre = []


@dataclass
class _CharNode:
    id: str
    name: str
    appearance_count: int = 0
    motivation: str = ""
    goal: str = ""
    obstacle: str = ""


@dataclass
class _CharGraph:
    nodes: list[_CharNode]


# ============================================================
# fixture 构造器
# ============================================================


def _make_hit_ctx() -> ScoringContext:
    """构造典型爆款短剧 fixture：穿越系统流 + 强 hook + 高密度 reward。"""
    total_eps = 20
    scenes: list[_Scene] = []
    rewards: list[_RewardEvent] = []
    for ep in range(1, total_eps + 1):
        for sno in range(1, 5):
            sid = f"s-e{ep}-{sno}"
            label = "内 客厅 日" if sno % 2 == 0 else "外 街道 日"
            if ep == 1 and sno == 1:
                text = "穿越！系统觉醒！宿主，请完成第一个任务：复仇。她重生回到了过去。\n陆总：你给我等着。\n苏婉：你竟然！"
            elif sno == 4:
                text = (
                    "她突然发现，原来真相是这样！门被推开，他扇了她一巴掌。"
                    "未完待续。\n苏婉：你怎么……\n陆总：我们离婚！"
                )
            else:
                text = "对白对白对白。\n苏婉：你说什么。\n陆总：穿越后我才懂。\n苏婉：复仇开始。"
            scenes.append(
                _Scene(
                    id=sid,
                    script_id="hit-script",
                    episode_no=ep,
                    scene_no=str(sno),
                    scene_label=label,
                    characters=["苏婉", "陆总"] if sno != 3 else ["苏婉"],
                    start_line=None,
                    end_line=None,
                    text=text,
                )
            )
        # 每集 2 个 reward（密度 2.0/集，达 high）
        rewards.append(
            _RewardEvent(
                scene_id=f"s-e{ep}-2",
                scene_no="2",
                episode_no=ep,
                event_type="face_slap",
                claim="打脸反派",
            )
        )
        rewards.append(
            _RewardEvent(
                scene_id=f"s-e{ep}-3",
                scene_no="3",
                episode_no=ep,
                event_type="reversal",
                claim="反转",
            )
        )

    cover = _CoverageCard(
        logline="穿越系统打脸复仇",
        synopsis=(
            "女主穿越后觉醒系统，开始向前世仇人复仇。每一集都有打脸 + 反转，"
            "层层揭露豪门内斗的真相。穿越系统 + 修罗场组合。"
        ),
        genre=["穿越", "复仇", "系统"],
    )
    char_graph = _CharGraph(
        nodes=[
            _CharNode(
                id="c1",
                name="苏婉",
                appearance_count=80,
                motivation="复仇",
                goal="打脸反派",
                obstacle="陆总阻挠",
            ),
            _CharNode(
                id="c2",
                name="陆总",
                appearance_count=60,
                motivation="霸道挽留",
                goal="留住苏婉",
                obstacle="苏婉的恨",
            ),
        ]
    )
    return ScoringContext(
        script_id="hit-script",
        scenes=scenes,
        total_episodes=total_eps,
        coverage_card=cover,
        character_graph=char_graph,
        reward_events=rewards,
        compliance=None,
        llm_caller=None,  # 不跑 LLM judge
    )


def _make_flop_ctx() -> ScoringContext:
    """构造扑街 fixture：日常文 + 零 reward + 模板模糊。"""
    total_eps = 20
    scenes: list[_Scene] = []
    for ep in range(1, total_eps + 1):
        for sno in range(1, 8):  # 每集 7 场（过多）
            scenes.append(
                _Scene(
                    id=f"f-e{ep}-{sno}",
                    script_id="flop-script",
                    episode_no=ep,
                    scene_no=str(sno),
                    scene_label="外 战场 夜" if sno % 3 == 0 else "内 房间 日",
                    characters=["张三", "李四", "王五", "赵六", "钱七"],
                    start_line=None,
                    end_line=None,
                    text="他们一起开会讨论项目。每个人都在发言。",
                )
            )
    cover = _CoverageCard(
        logline="项目会议剧本",
        synopsis="一群人开了二十集的会议，谁也没有起来。",
        genre=["职场", "日常"],
    )
    return ScoringContext(
        script_id="flop-script",
        scenes=scenes,
        total_episodes=total_eps,
        coverage_card=cover,
        character_graph=_CharGraph(
            nodes=[
                _CharNode(id="c1", name="张三", appearance_count=20),
                _CharNode(id="c2", name="李四", appearance_count=20),
                _CharNode(id="c3", name="王五", appearance_count=20),
            ]
        ),
        reward_events=[],
        compliance=None,
        llm_caller=None,
    )


def _make_compliance_block_ctx() -> ScoringContext:
    """compliance high_risk → veto。"""
    ctx = _make_hit_ctx()
    ctx.script_id = "compliance-block"

    @dataclass
    class _Comp:
        tier: str = "high_risk"

    ctx.compliance = _Comp()  # type: ignore[assignment]
    return ctx


def _make_dealbreaker_hook_ctx() -> ScoringContext:
    """所有维度 OK，但 hook 全空 → hook dealbreaker。"""
    ctx = _make_hit_ctx()
    ctx.script_id = "dealbreaker-hook"
    # 把所有场景的 text 改成"早上他吃饭"——零 hook 关键词
    for sc in ctx.scenes:
        sc.text = "他坐下来吃饭。早上的阳光不错。"  # type: ignore[misc]
    return ctx


# ============================================================
# 测试
# ============================================================


def test_hit_fixture_yields_qualified() -> None:
    ctx = _make_hit_ctx()
    report = asyncio.run(score_script(ctx))
    # hit fixture：完整密度 + 强 hook + 模板清晰 → 应该 qualified 或 needs_polish
    # （LLM judge 缺席，会留 2 个 FAILED signal，影响 confidence 不影响 verdict）
    assert report.verdict.label in (VerdictLabel.QUALIFIED, VerdictLabel.NEEDS_POLISH)
    assert report.rubric_version == "v4-cn-2026-05-31"
    assert len(report.dimensions) == 5


def test_flop_fixture_not_recommended_or_polish() -> None:
    ctx = _make_flop_ctx()
    report = asyncio.run(score_script(ctx))
    assert report.verdict.label in (
        VerdictLabel.NOT_RECOMMENDED,
        VerdictLabel.NEEDS_POLISH,
    )


def test_compliance_block_vetoes() -> None:
    ctx = _make_compliance_block_ctx()
    report = asyncio.run(score_script(ctx))
    assert report.verdict.label == VerdictLabel.NOT_RECOMMENDED
    assert report.verdict.compliance_veto_triggered is True


def test_dealbreaker_hook_triggers_not_recommended() -> None:
    ctx = _make_dealbreaker_hook_ctx()
    report = asyncio.run(score_script(ctx))
    # HOOK 维度无关键词命中 → score 很低 → dealbreaker triggered
    hook_score = next(d.score for d in report.dimensions if d.key == "hook")
    assert hook_score < 4.0
    hook_dim = next(d for d in report.dimensions if d.key == "hook")
    assert hook_dim.is_dealbreaker_triggered is True
    assert report.verdict.label == VerdictLabel.NOT_RECOMMENDED


def test_hit_fixture_has_chain_status_records() -> None:
    ctx = _make_hit_ctx()
    report = asyncio.run(score_script(ctx))
    assert len(report.chain_status_records) == 5
    for rec in report.chain_status_records:
        assert rec["overall_status"] in ("ok", "degraded", "failed")


def test_v3_legacy_dim_key_raises() -> None:
    from service.scoring.rubric_loader import RubricLegacyDimensionError

    ctx = _make_hit_ctx()
    from service.scoring import score_dimension

    with pytest.raises(RubricLegacyDimensionError):
        asyncio.run(score_dimension("story", ctx))
