from service.score_registry.loader import DimensionConfig, RubricConfig, SignalConfig
from service.script_tools.percentile_tier import resolve_industry_proxy_tier, resolve_tier


def _rubric() -> RubricConfig:
    return RubricConfig(
        rubric_id="test",
        status="industry_proxy",
        description="",
        score_ver="test",
        breaking=False,
        base_weight={"story": 1.0},
        genre_multiplier={"default": {"story": 1.0}},
        tier_cuts={"default": {"story": {"p25": 4.0, "p50": 6.0, "p75": 8.0}}},
        dimensions=(
            DimensionConfig(
                id="story",
                signals=(SignalConfig(id="a", weight_in_dim=1.0, source="rule", primary=True),),
            ),
        ),
        llm_bundles=tuple(),
    )


def test_resolve_tier_by_score_cutoffs() -> None:
    rubric = _rubric()
    excellent = resolve_tier(rubric, dimension="story", score=8.2, genre_scope="default", sample_size=120)
    good = resolve_tier(rubric, dimension="story", score=6.4, genre_scope="default", sample_size=120)
    weak = resolve_tier(rubric, dimension="story", score=4.3, genre_scope="default", sample_size=120)
    poor = resolve_tier(rubric, dimension="story", score=2.2, genre_scope="default", sample_size=120)
    assert excellent.tier == "excellent"
    assert good.tier == "good"
    assert weak.tier == "weak"
    assert poor.tier == "poor"
    assert excellent.cuts == {"p25": 4.0, "p50": 6.0, "p75": 8.0}


def test_resolve_tier_low_sample_degrades_confidence() -> None:
    rubric = _rubric()
    result = resolve_tier(rubric, dimension="story", score=8.5, genre_scope="default", sample_size=12)
    assert result.tier == "excellent"
    assert result.confidence == "low"
    assert result.cuts["p75"] == 8.0


def test_resolve_industry_proxy_tier() -> None:
    assert resolve_industry_proxy_tier(value=None) == "insufficient"
    assert resolve_industry_proxy_tier(value=8.5) == "excellent"
    assert resolve_industry_proxy_tier(value=6.5) == "good"
    assert resolve_industry_proxy_tier(value=4.2) == "weak"
    assert resolve_industry_proxy_tier(value=2.1) == "poor"
