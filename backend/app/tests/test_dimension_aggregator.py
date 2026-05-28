from service.score_registry.loader import DimensionConfig, RubricConfig, SignalConfig
from service.script_tools.dimension_aggregator import aggregate
from service.script_tools.signal_catalog import SignalValue


def _rubric() -> RubricConfig:
    return RubricConfig(
        rubric_id="test",
        status="experimental",
        description="",
        score_ver="test",
        breaking=False,
        base_weight={"story": 1.0},
        genre_multiplier={"default": {"story": 1.0}},
        tier_cuts={"default": {"story": {"p25": 4.0, "p50": 6.0, "p75": 8.0}}},
        dimensions=(
            DimensionConfig(
                id="story",
                signals=(
                    SignalConfig(id="a", weight_in_dim=0.4, source="rule", primary=True),
                    SignalConfig(id="b", weight_in_dim=0.6, source="rule", primary=False),
                ),
            ),
        ),
        llm_bundles=tuple(),
    )


def test_coverage_below_threshold_marks_insufficient() -> None:
    rubric = _rubric()
    signal_values = {
        "a": SignalValue(key="a", value=1, score=8.0, source="rule", confidence=0.9),
        # b missing => coverage = 0.4
    }
    out = aggregate(rubric, signal_values, coverage_threshold=0.5)
    assert len(out) == 1
    story = out[0]
    assert story.dimension == "story"
    assert story.score is None
    assert story.tier == "insufficient"
    assert story.coverage_ratio < 0.5


def test_weighted_score_and_confidence_label() -> None:
    rubric = _rubric()
    signal_values = {
        "a": SignalValue(key="a", value=1, score=8.0, source="rule", confidence=0.8),
        "b": SignalValue(key="b", value=1, score=6.0, source="rule", confidence=0.7),
    }
    out = aggregate(rubric, signal_values, coverage_threshold=0.5)
    story = out[0]
    assert story.score is not None
    # weighted score = 8*0.4 + 6*0.6 = 6.8
    assert abs(story.score - 6.8) < 1e-6
    assert story.confidence in {"medium", "high"}
    assert story.coverage_ratio == 1.0
    assert len(story.top_signals) == 2
    assert story.top_signals[0]["signal_key"] == "b"
    assert story.top_signals[1]["signal_key"] == "a"
