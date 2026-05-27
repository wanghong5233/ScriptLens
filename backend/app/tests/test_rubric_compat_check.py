from service.score_registry.compat_check import compare_rubrics
from service.score_registry.loader import DimensionConfig, LlmBundleConfig, RubricConfig, SignalConfig


def _cfg(
    rubric_id: str,
    dims: dict[str, list[tuple[str, float]]],
    *,
    bundles: list[str] | None = None,
) -> RubricConfig:
    dimensions = tuple(
        DimensionConfig(
            id=dim_id,
            signals=tuple(
                SignalConfig(id=signal_id, weight_in_dim=weight, source="rule", primary=False)
                for signal_id, weight in signals
            ),
        )
        for dim_id, signals in dims.items()
    )
    bundle_ids = bundles or []
    llm_bundles = tuple(
        LlmBundleConfig(
            id=bundle_id,
            scope="script",
            signals=("dummy_signal",),
            prompt="score_registry/prompts/v3/story_llm.jinja",
        )
        for bundle_id in bundle_ids
    )
    return RubricConfig(
        rubric_id=rubric_id,
        status="experimental",
        description="",
        score_ver=rubric_id,
        breaking=False,
        base_weight={dim_id: 1.0 for dim_id in dims.keys()},
        genre_multiplier={"default": {dim_id: 1.0 for dim_id in dims.keys()}},
        tier_cuts={"default": {dim_id: {"p25": 4.0, "p50": 6.0, "p75": 8.0} for dim_id in dims.keys()}},
        dimensions=dimensions,
        llm_bundles=llm_bundles,
    )


def test_add_dimension_is_backward_compatible() -> None:
    baseline = _cfg("base", {"story": [("a", 1.0)]})
    candidate = _cfg("cand", {"story": [("a", 1.0)], "dialogue": [("b", 1.0)]})
    result = compare_rubrics(baseline, candidate, mode="BACKWARD", allow_breaking=False)
    assert result.compatible is True
    assert any(issue.kind == "add_dim" and issue.dimension == "dialogue" for issue in result.issues)


def test_remove_dimension_is_incompatible_without_breaking() -> None:
    baseline = _cfg("base", {"story": [("a", 1.0)], "dialogue": [("b", 1.0)]})
    candidate = _cfg("cand", {"story": [("a", 1.0)]})
    result = compare_rubrics(baseline, candidate, mode="BACKWARD", allow_breaking=False)
    assert result.compatible is False
    assert any(issue.kind == "remove_dim" and issue.dimension == "dialogue" for issue in result.issues)


def test_weight_change_is_reported() -> None:
    baseline = _cfg("base", {"story": [("a", 0.5), ("b", 0.5)]})
    candidate = _cfg("cand", {"story": [("a", 0.7), ("b", 0.3)]})
    result = compare_rubrics(baseline, candidate, mode="BACKWARD", allow_breaking=False)
    assert any(issue.kind == "weight_change" and issue.signal == "a" for issue in result.issues)
    assert any(issue.kind == "weight_change" and issue.signal == "b" for issue in result.issues)


def test_allow_breaking_overrides_incompatible_changes() -> None:
    baseline = _cfg("base", {"story": [("a", 1.0)]}, bundles=["bundle_a"])
    candidate = _cfg("cand", {"story": [("a", 1.0)]}, bundles=[])
    result = compare_rubrics(baseline, candidate, mode="BACKWARD", allow_breaking=True)
    assert result.compatible is True
    assert any(issue.kind == "remove_bundle" for issue in result.issues)
