from service.score_registry.loader import load_rubric
from service.script_tools.signal_catalog import SignalContext, compute_rule_signals
from service.script_tools.signal_catalog.rule_signals.pacing import narrative_intensity


def _build_ctx() -> SignalContext:
    plot_units = [
        {"id": "u1", "episode_no": 1, "idx": 1, "summary": "主角被当众羞辱后立誓反击"},
        {"id": "u2", "episode_no": 1, "idx": 2, "summary": "主角回忆前史，信息铺垫"},
        {"id": "u3", "episode_no": 2, "idx": 3, "summary": "主角反击成功并揭露反派阴谋"},
    ]
    plot_unit_tags = [
        {"plot_unit_id": "u1", "dim": "plot_hook", "value": "identity_reveal"},
        {"plot_unit_id": "u1", "dim": "conflict_type", "value": "revenge"},
        {"plot_unit_id": "u1", "dim": "payoff_type", "value": "face_slap"},
        {"plot_unit_id": "u1", "dim": "emotional_driver", "value": "anger"},
        {"plot_unit_id": "u1", "dim": "dialogue_density", "value": "dense"},
        {"plot_unit_id": "u1", "dim": "story_stage", "value": "trigger"},
        {"plot_unit_id": "u2", "dim": "plot_hook", "value": "none"},
        {"plot_unit_id": "u2", "dim": "conflict_type", "value": "none"},
        {"plot_unit_id": "u2", "dim": "payoff_type", "value": "none"},
        {"plot_unit_id": "u2", "dim": "emotional_driver", "value": "none"},
        {"plot_unit_id": "u2", "dim": "dialogue_density", "value": "sparse"},
        {"plot_unit_id": "u2", "dim": "story_stage", "value": "setup"},
        {"plot_unit_id": "u3", "dim": "plot_hook", "value": "reversal"},
        {"plot_unit_id": "u3", "dim": "conflict_type", "value": "confrontation"},
        {"plot_unit_id": "u3", "dim": "payoff_type", "value": "revenge"},
        {"plot_unit_id": "u3", "dim": "emotional_driver", "value": "justice"},
        {"plot_unit_id": "u3", "dim": "dialogue_density", "value": "moderate"},
        {"plot_unit_id": "u3", "dim": "story_stage", "value": "climax"},
    ]
    plot_tags_by_unit = {
        "u1": {
            "plot_hook": ["identity_reveal"],
            "conflict_type": ["revenge"],
            "payoff_type": ["face_slap"],
            "emotional_driver": ["anger"],
            "dialogue_density": ["dense"],
            "story_stage": ["trigger"],
        },
        "u2": {
            "plot_hook": ["none"],
            "conflict_type": ["none"],
            "payoff_type": ["none"],
            "emotional_driver": ["none"],
            "dialogue_density": ["sparse"],
            "story_stage": ["setup"],
        },
        "u3": {
            "plot_hook": ["reversal"],
            "conflict_type": ["confrontation"],
            "payoff_type": ["revenge"],
            "emotional_driver": ["justice"],
            "dialogue_density": ["moderate"],
            "story_stage": ["climax"],
        },
    }
    return SignalContext(
        script_id="script-1",
        script_meta={"id": "script-1", "title": "示例短剧", "total_episodes": 2, "total_scenes": 6},
        plot_units=plot_units,
        plot_unit_tags=plot_unit_tags,
        script_tags=[{"dim": "drama_tags", "value": "战神"}],
        episode_tags=[],
        character_entities=[
            {"id": "c1", "role": "protagonist", "agency_level": "high"},
            {"id": "c2", "role": "antagonist", "agency_level": "medium"},
        ],
        character_relationships=[
            {"id": "r1", "polarity": "negative", "dynamic_arc": "rising"},
        ],
        scenes=[
            {"id": "s1", "episode_no": 1, "scene_no": "1", "text": "主角被羞辱后反击。"},
            {"id": "s2", "episode_no": 2, "scene_no": "2", "text": "主角揭露真相。"},
        ],
        drama_tags=["战神", "复仇"],
        plot_tags_by_unit=plot_tags_by_unit,
        script_tag_map={"drama_tags": ["战神", "复仇"]},
        episode_tag_map={},
    )


def test_narrative_intensity_formula_locked() -> None:
    ctx = _build_ctx()
    # hook + conflict + payoff + emotional driver = 2 + 2 + 3 + 1 = 8
    assert narrative_intensity(ctx, "u1") == 8
    assert narrative_intensity(ctx, "u2") == 0


def test_compute_rule_signals_returns_registered_values() -> None:
    rubric = load_rubric("v3.0.0")
    ctx = _build_ctx()
    signals = compute_rule_signals(rubric, ctx)
    assert "structural_completeness" in signals
    assert "protagonist_agency" in signals
    assert "opening_speed" in signals
    assert "dialogue_density" in signals

    opening = signals["opening_speed"]
    assert opening.score is not None
    assert opening.source == "rule"
    assert opening.weight_in_dim is not None
    assert opening.primary_dimension == "pacing"
