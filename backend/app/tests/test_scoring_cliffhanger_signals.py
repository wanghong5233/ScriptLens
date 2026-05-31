"""HOOK / MONETIZATION cliffhanger 信号迁移测试（v4.1）。

Wave: cliffhanger-extractor (2026-05-31)

新版 cliffhanger 信号数据源：上游 `cliffhanger_extractor` 的 LLM 二级判定结果
（CliffhangerEvent[]），不再 naive 关键词扫。

本测试只覆盖"signal 函数对 ctx.cliffhangers 的消费"，不跑真 LLM。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from service.script_tools.cliffhanger_extractor import (
    CLIFF_TYPE_CN_LABELS,
    CliffhangerEvent,
)
from service.scoring.dimensions.hook import _signal_episode_end_cliffhanger
from service.scoring.dimensions.monetization import _signal_end_hook, _signal_paywall
from service.scoring.framework import (
    ScoringContext,
    SignalSource,
    SignalStatus,
)
from service.scoring.rubric_loader import load_rubric


# ============================================================
# Scene stub
# ============================================================


@dataclass
class _Scene:
    id: str
    script_id: str
    episode_no: Optional[int]
    scene_no: str
    scene_label: str
    characters: list[str] = field(default_factory=list)
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    text: str = ""

    @property
    def char_count(self) -> int:
        return len(self.text or "")


def _make_scenes(total_episodes: int) -> list[_Scene]:
    out: list[_Scene] = []
    for ep in range(1, total_episodes + 1):
        for sno in range(1, 4):
            out.append(
                _Scene(
                    id=f"s-e{ep}-{sno}",
                    script_id="test",
                    episode_no=ep,
                    scene_no=str(sno),
                    scene_label="内 客厅 日",
                    text="对白对白",
                )
            )
    return out


def _make_cliff(
    episode_no: int, cliff_type: str = "physical_danger"
) -> CliffhangerEvent:
    return CliffhangerEvent(
        scene_id=f"s-e{episode_no}-3",
        scene_no="3",
        episode_no=episode_no,
        cliff_type=cliff_type,
        claim=f"第 {episode_no} 集集末留钩",
        quote_verbatim="她推开门，发现他倒在血泊里",
        quote_verified=True,
        confidence="high",
    )


def _load_signal_cfg(dim_key: str, signal_key: str):
    rubric = load_rubric()
    dim = rubric.dimensions[dim_key]
    return next(s for s in dim.signals if s.key == signal_key)


# ============================================================
# HOOK.episode_end_cliffhanger_rate
# ============================================================


# 业内 SOP（抖音红果 / 快手星芒 / 九州文化 / 点众选品报告）：
# detail 字符串严禁出现技术主语 "AI 判读" / "AI 在 / AI 给" / "LLM" 等，
# 用户视角只关心剧本事实，不关心评分链路实现细节。
_FORBIDDEN_DETAIL_PHRASES: tuple[str, ...] = (
    "AI 判读",
    "AI 在",
    "AI 给",
    "LLM",
    "cliffhanger",  # 5 类 cliff_type 中文化后严禁出现英文词
)


def _assert_clean_detail(detail: str) -> None:
    for phrase in _FORBIDDEN_DETAIL_PHRASES:
        assert phrase.lower() not in detail.lower(), (
            f"detail 含禁用技术词 {phrase!r}；实际 detail={detail!r}"
        )


def test_episode_end_cliffhanger_rate_uses_extractor_data():
    """信号 source = HYBRID，detail 含中文 cliff_type，detail 严禁含技术主语。"""
    cfg = _load_signal_cfg("hook", "episode_end_cliffhanger_rate")
    ctx = ScoringContext(
        script_id="t",
        scenes=_make_scenes(10),
        total_episodes=10,
        cliffhangers=[
            _make_cliff(1, "physical_danger"),
            _make_cliff(2, "false_defeat"),
            _make_cliff(3, "emotional_reveal"),
            _make_cliff(4, "physical_danger"),
        ],
    )
    sig = _signal_episode_end_cliffhanger(ctx, cfg)
    assert sig.source == SignalSource.HYBRID
    assert sig.status == SignalStatus.COMPUTED
    assert sig.raw_value == pytest.approx(0.4)
    # detail 必须含中文 cliff_type label 之一
    assert any(label in sig.detail for label in CLIFF_TYPE_CN_LABELS.values()), (
        f"detail 应含中文 cliff_type，实际={sig.detail!r}"
    )
    _assert_clean_detail(sig.detail)


def test_episode_end_cliffhanger_rate_zero_cliffs():
    """无 cliffhanger 时 raw=0，detail 用中性事实陈述（不带技术主语）。"""
    cfg = _load_signal_cfg("hook", "episode_end_cliffhanger_rate")
    ctx = ScoringContext(
        script_id="t",
        scenes=_make_scenes(5),
        total_episodes=5,
        cliffhangers=[],
    )
    sig = _signal_episode_end_cliffhanger(ctx, cfg)
    assert sig.raw_value == pytest.approx(0.0)
    assert "未出现" in sig.detail or "未发现" in sig.detail
    _assert_clean_detail(sig.detail)


# ============================================================
# MONETIZATION.paywall_cliffhanger_strength
# ============================================================


def test_paywall_cliffhanger_strength_picks_paywall_ep():
    """付费拐点集若有 physical_danger 留钩，raw = 1.0；detail 含 claim 文本。"""
    cfg = _load_signal_cfg("monetization", "paywall_cliffhanger_strength")
    # 默认 paywall_min/max=15/20 → mid=17
    ctx = ScoringContext(
        script_id="t",
        scenes=_make_scenes(20),
        total_episodes=20,
        cliffhangers=[
            _make_cliff(17, "physical_danger"),
            _make_cliff(5, "mystery_setup"),  # 不在付费拐点集
        ],
    )
    sig = _signal_paywall(ctx, cfg)
    assert sig.source == SignalSource.HYBRID
    assert sig.raw_value == pytest.approx(1.0)
    assert "危机时刻" in sig.detail
    _assert_clean_detail(sig.detail)


def test_paywall_cliffhanger_strength_weight_by_type():
    """不同 cliff_type 应用不同权重（参考字节 WebConf 2026 §3.2）。"""
    cfg = _load_signal_cfg("monetization", "paywall_cliffhanger_strength")
    # mystery_setup 是 5 类中最弱的
    ctx = ScoringContext(
        script_id="t",
        scenes=_make_scenes(20),
        total_episodes=20,
        cliffhangers=[_make_cliff(17, "mystery_setup")],
    )
    sig = _signal_paywall(ctx, cfg)
    assert sig.raw_value == pytest.approx(0.55)  # type_weight_mystery_setup


def test_paywall_cliffhanger_strength_no_cliff_at_paywall():
    """付费拐点集无 cliffhanger → raw=0，detail 提示付费转化风险。"""
    cfg = _load_signal_cfg("monetization", "paywall_cliffhanger_strength")
    ctx = ScoringContext(
        script_id="t",
        scenes=_make_scenes(20),
        total_episodes=20,
        cliffhangers=[_make_cliff(10, "physical_danger")],  # 不在 paywall_ep
    )
    sig = _signal_paywall(ctx, cfg)
    assert sig.raw_value == pytest.approx(0.0)
    assert "未出现强留钩" in sig.detail or "付费转化风险" in sig.detail
    _assert_clean_detail(sig.detail)


# ============================================================
# MONETIZATION.episode_end_hook_grade
# ============================================================


def test_episode_end_hook_grade_uses_extractor_data():
    cfg = _load_signal_cfg("monetization", "episode_end_hook_grade")
    ctx = ScoringContext(
        script_id="t",
        scenes=_make_scenes(10),
        total_episodes=10,
        cliffhangers=[_make_cliff(i, "emotional_reveal") for i in range(1, 8)],
    )
    sig = _signal_end_hook(ctx, cfg)
    assert sig.source == SignalSource.HYBRID
    assert sig.raw_value == pytest.approx(0.7)
    assert "真相揭露" in sig.detail
    _assert_clean_detail(sig.detail)
