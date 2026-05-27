from service.score_registry.loader import (
    get_genre_multiplier,
    get_tier_cuts,
    list_llm_bundles,
    list_signals,
    load_prompt_by_bundle,
    load_rubric,
)


def test_load_rubric_v3_core_shape() -> None:
    rubric = load_rubric("v3.0.0")
    assert rubric.rubric_id == "v3.0.0"
    assert "dialogue" in rubric.all_dimensions
    assert len(rubric.all_dimensions) == 6
    assert len(rubric.llm_bundles) == 5


def test_list_signals_and_bundles() -> None:
    story_signals = list_signals("v3.0.0", "story")
    story_ids = {signal.id for signal in story_signals}
    assert "structural_completeness" in story_ids
    assert "logline_clear" in story_ids

    bundles = list_llm_bundles("v3.0.0", scope="script")
    assert bundles
    assert any(bundle.id == "v3_story_llm" for bundle in bundles)


def test_load_prompt_by_bundle() -> None:
    prompt = load_prompt_by_bundle("v3.0.0", "v3_story_llm")
    assert "JSON" in prompt
    assert "signals" in prompt


def test_default_genre_multiplier_and_tier_cuts() -> None:
    multiplier = get_genre_multiplier("v3.0.0", "unknown_genre")
    cuts = get_tier_cuts("v3.0.0", "unknown_genre")
    assert "story" in multiplier
    assert "story" in cuts
    assert cuts["story"]["p25"] <= cuts["story"]["p50"] <= cuts["story"]["p75"]
