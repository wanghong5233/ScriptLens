from typing import get_args

from schemas import plot_tags


def test_v0_literals_include_expected_values() -> None:
    assert "identity_reveal" in get_args(plot_tags.PlotHook)
    assert "dense" in get_args(plot_tags.DialogueDensity)
    assert "power_payoff" in get_args(plot_tags.BusinessContentArchetype)


def test_v1_literals_include_expected_values() -> None:
    assert "male_lead" in get_args(plot_tags.GenderAxis)
    assert "war_god_return" in get_args(plot_tags.ProtagonistArchetype)
    assert "love_triangle" in get_args(plot_tags.RelationshipTriangle)


def test_v2_literals_include_expected_values() -> None:
    assert "scene_locale_type" not in get_args(plot_tags.SceneLocaleType)  # sanity: values not dim names
    assert "modern_indoor" in get_args(plot_tags.SceneLocaleType)
    assert "close_up" in get_args(plot_tags.ShotSuggestion)


def test_tag_item_model_defaults() -> None:
    item = plot_tags.TagItem(dim="plot_hook", value="identity_reveal")
    assert item.source == "llm"
    assert item.score is None

