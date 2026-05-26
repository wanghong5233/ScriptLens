from service.tag_registry.loader import get_prompt_ver, load_prompt, load_tag_set
from service.tag_registry.validator import validate, validate_tagset


def test_load_tag_set_counts() -> None:
    v0 = load_tag_set("v0.1.0")
    v1 = load_tag_set("v1.0.0")
    v2 = load_tag_set("v2.0.0")
    assert len(v0.all_dims) == 17
    assert len(v1.all_dims) == 36
    assert len(v2.all_dims) == 42


def test_load_prompt_for_dim() -> None:
    content = load_prompt("v0.1.0", "plot_hook")
    assert "plot_unit" in content
    assert "JSON" in content


def test_prompt_ver_shape() -> None:
    ver = get_prompt_ver("v0.1.0", "plot_hook", variant="b")
    assert ver.startswith("v0.1.0:")
    assert ver.endswith(":b")


def test_validator_for_closed_enum() -> None:
    validate("v0.1.0", "plot_hook", "identity_reveal")
    errors = validate_tagset(
        "v0.1.0",
        {"plot_hook": "not_exist"},
        dims=["plot_hook"],
        allow_partial=True,
    )
    assert "plot_hook" in errors


def test_validator_for_open_enum() -> None:
    validate("v0.1.0", "drama_tags", ["战神", "我的新标签"])
