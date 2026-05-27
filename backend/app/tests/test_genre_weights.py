from service.score_registry.loader import DimensionConfig, RubricConfig, SignalConfig
from service.script_tools.dimension_aggregator import DimensionScore
from service.script_tools.genre_weights import apply_genre_weights, infer_genre_scope


def _rubric() -> RubricConfig:
    return RubricConfig(
        rubric_id="test",
        status="industry_proxy",
        description="",
        score_ver="test",
        breaking=False,
        base_weight={"story": 0.5, "dialogue": 0.5},
        genre_multiplier={
            "default": {"story": 1.0, "dialogue": 1.0},
            "战神_复仇_逆袭": {"story": 1.2, "dialogue": 0.8},
        },
        tier_cuts={
            "default": {
                "story": {"p25": 4.0, "p50": 6.0, "p75": 8.0},
                "dialogue": {"p25": 4.0, "p50": 6.0, "p75": 8.0},
            }
        },
        dimensions=(
            DimensionConfig(
                id="story",
                signals=(SignalConfig(id="a", weight_in_dim=1.0, source="rule", primary=True),),
            ),
            DimensionConfig(
                id="dialogue",
                signals=(SignalConfig(id="b", weight_in_dim=1.0, source="rule", primary=True),),
            ),
        ),
        llm_bundles=tuple(),
    )


def test_infer_genre_scope() -> None:
    assert infer_genre_scope(["战神", "复仇"]) == "战神_复仇_逆袭"
    assert infer_genre_scope(["甜宠", "爱情"]) == "甜宠_爱情"
    assert infer_genre_scope(["未知标签"]) == "default"


def test_apply_genre_weights_normalizes_usable_dimensions() -> None:
    rubric = _rubric()
    dim_scores = [
        DimensionScore(
            dimension="story",
            score=8.0,
            tier="good",
            confidence="high",
            coverage_ratio=1.0,
            reason="",
            signal_refs=[],
            primary_dimension="story",
        ),
        DimensionScore(
            dimension="dialogue",
            score=6.0,
            tier="good",
            confidence="medium",
            coverage_ratio=1.0,
            reason="",
            signal_refs=[],
            primary_dimension="dialogue",
        ),
    ]
    weighted = apply_genre_weights(rubric, dim_scores, genre_scope="战神_复仇_逆袭")
    assert weighted.genre == "战神_复仇_逆袭"
    assert weighted.overall_score is not None
    # story weight should be higher after genre multiplier.
    assert weighted.normalized_weights["story"] > weighted.normalized_weights["dialogue"]
