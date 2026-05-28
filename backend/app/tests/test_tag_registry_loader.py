from service.tag_registry.loader import (
    get_prompt_ver,
    list_bundles,
    load_bundle,
    load_prompt,
    load_prompt_by_bundle,
    load_tag_set,
)
from service.tag_registry.validator import validate, validate_tagset


def test_load_tag_set_counts() -> None:
    script = load_tag_set("script")
    assert len(script.all_dims) == 43
    assert len(script.bundles) == 12


def test_load_prompt_for_dim() -> None:
    content = load_prompt("script", "plot_hook")
    assert "plot_unit" in content
    assert "JSON" in content


def test_bundle_apis() -> None:
    bundle = load_bundle("script", "plot_core")
    assert bundle.scope == "plot_unit"
    assert "plot_hook" in bundle.dims
    bundles = list_bundles("script", scope="relationship")
    assert bundles and bundles[0].id == "relationship_attrs"
    prompt = load_prompt_by_bundle("script", "relationship_attrs")
    assert "关系" in prompt


def test_prompt_ver_shape() -> None:
    ver = get_prompt_ver("script", "plot_hook", variant="b")
    assert ver.startswith("script:")
    assert ver.endswith(":b")


def test_validator_for_closed_enum() -> None:
    validate("script", "plot_hook", "identity_reveal")
    errors = validate_tagset(
        "script",
        {"plot_hook": "not_exist"},
        dims=["plot_hook"],
        allow_partial=True,
    )
    assert "plot_hook" in errors


def test_validator_for_open_enum() -> None:
    validate("script", "prop_focus", ["玉佩", "我的新道具"])
