"""beat_chain 单元测试。

聚焦规则层：候选锚点选取、rule fallback、3 幕兜底——这些是不依赖 LLM
也必须正确的工程契约。LLM enrichment 路径（_enrich_via_llm）有 LLM 调用，
留给集成测试。
"""

from __future__ import annotations

from typing import List

from service.script_tools import beat_chain as bc
from service.script_tools.scene_repo import Scene


def _scene(*, sid: str, no: str, episode: int = 1, label: str = "", text: str = "") -> Scene:
    return Scene(
        id=sid,
        script_id="s-1",
        episode_no=episode,
        scene_no=no,
        scene_label=label or f"E{episode:02d}-S{no}",
        characters=[],
        start_line=None,
        end_line=None,
        text=text,
    )


def _reward(*, scene_id: str, count: int = 1):  # type: ignore[no-untyped-def]
    """轻量构造 RewardEvent 列表——同一 scene_id 重复 count 次模拟"reward 强度"。

    beat_chain 用 ``getattr(ev, 'score', 1.0)`` 取强度；当前 RewardEvent 数据类
    没有 score 字段，会落到默认 1.0 → 同一 scene_id 多次出现等价于"score 累计"。
    """
    from service.script_tools.reward_extractor import RewardEvent

    return [
        RewardEvent(
            scene_id=scene_id,
            scene_no="x",
            episode_no=None,
            event_type="reversal",
            evidence="test",
        )
        for _ in range(count)
    ]


def test_derive_candidate_anchors_returns_at_least_one_per_act_for_long_script() -> None:
    """长剧（20 场）应该 3 幕都拿到至少 1 个候选。"""
    scenes = [_scene(sid=f"sc-{i}", no=f"1-{i}") for i in range(20)]
    candidates = bc._derive_candidate_anchors(scenes, [])
    acts = {c.act for c in candidates}
    assert acts == {1, 2, 3}, f"expected 3 acts represented, got {acts}"
    # 每个候选都有真实 scene 引用
    for c in candidates:
        assert c.scene.id.startswith("sc-")
        assert c.seq >= 1
        assert c.type_hint in bc._ALLOWED_BEATS


def test_derive_candidate_anchors_handles_short_script() -> None:
    """很短的剧（3 场）也要保证 3 幕各有一锚（哪怕指向同一 scene）。"""
    scenes = [_scene(sid="sc-0", no="1-1"), _scene(sid="sc-1", no="1-2"), _scene(sid="sc-2", no="1-3")]
    candidates = bc._derive_candidate_anchors(scenes, [])
    acts = {c.act for c in candidates}
    assert 1 in acts and 3 in acts


def test_derive_candidate_anchors_returns_empty_for_too_few_scenes() -> None:
    """场太少 → 返回空，让上游走 rule_fallback 路径。"""
    assert bc._derive_candidate_anchors([], []) == []
    assert bc._derive_candidate_anchors([_scene(sid="sc-0", no="1-1")], []) == []


def test_derive_candidate_anchors_uses_reward_peak_for_climax() -> None:
    """climax 应优先取 act3 内 reward 累计最高的场。"""
    scenes = [_scene(sid=f"sc-{i}", no=f"1-{i}") for i in range(20)]
    # act3 = scenes[17:20] = sc-17 / sc-18 / sc-19；让 sc-17 reward 累计 5 次最高
    rewards = _reward(scene_id="sc-17", count=5) + _reward(scene_id="sc-19", count=1)
    candidates = bc._derive_candidate_anchors(scenes, rewards)
    climax = next((c for c in candidates if c.type_hint == "climax"), None)
    assert climax is not None
    assert climax.scene.id == "sc-17"
    # opening 永远是第一场
    opening = next((c for c in candidates if c.type_hint == "opening"), None)
    assert opening is not None
    assert opening.scene.id == "sc-0"


def test_rule_fallback_always_returns_three_acts_with_at_least_one_beat() -> None:
    """LLM 失败时的 rule fallback：永远返回 3 幕，且每幕至少 1 个 beat。"""
    scenes = [_scene(sid=f"sc-{i}", no=f"1-{i}", label=f"场{i}") for i in range(20)]
    sheet = bc._rule_fallback(scenes, [], reason="test")
    assert sheet.source == "rule_fallback"
    assert len(sheet.acts) == 3
    for act in sheet.acts:
        assert len(act.beats) >= 1, f"act {act.act} has 0 beats"
        for beat in act.beats:
            assert beat.anchor_scene_id.startswith("sc-")
            assert beat.summary  # 非空
            assert len(beat.summary) <= bc._SUMMARY_MAX_LEN
            assert beat.type in bc._ALLOWED_BEATS


def test_rule_fallback_handles_single_scene_gracefully() -> None:
    """极端：1 场也不抛异常，至少给 act1 留 opening。"""
    sheet = bc._rule_fallback([_scene(sid="sc-only", no="1-1")], [], reason="single")
    # 不要求一定 3 幕 beat 满，只要 不崩 + act1 有 opening 就算合格
    assert sheet.source == "rule_fallback"
    flat_beats = [b for a in sheet.acts for b in a.beats]
    assert any(b.anchor_scene_id == "sc-only" for b in flat_beats)


def test_build_acts_titles_use_chinese_defaults() -> None:
    candidates = bc._derive_candidate_anchors(
        [_scene(sid=f"sc-{i}", no=f"1-{i}") for i in range(12)], []
    )
    acts = bc._build_acts(candidates, beats_by_act={1: [], 2: [], 3: []})
    titles = [a.title for a in acts]
    assert titles == ["开局", "发展", "收束"]
