from service.tag_registry.compat_check import compare_tag_sets
from service.tag_registry.loader import DimConfig, TagSetConfig


def _cfg(version: str, dims: dict[str, list[tuple[str, tuple[str, ...]]]]) -> TagSetConfig:
    scope_to_dims = {
        scope: tuple(
            DimConfig(scope=scope, dim=dim, cardinality="single", open_enum=False, values=values)
            for dim, values in entries
        )
        for scope, entries in dims.items()
    }
    return TagSetConfig(
        version=version,
        description="",
        prompt_ver=version,
        breaking=False,
        scope_to_dims=scope_to_dims,
        bundles=tuple(),
        dim_to_bundle_id={},
    )


def test_add_dim_backward_compatible() -> None:
    base = _cfg("base", {"script": [("a", ("x", "y"))]})
    cand = _cfg("cand", {"script": [("a", ("x", "y")), ("b", ("m", "n"))]})
    result = compare_tag_sets(base, cand, mode="BACKWARD", allow_breaking=False)
    assert result.compatible is True
    assert any(i.kind == "add_dim" and i.dim == "b" for i in result.issues)


def test_remove_dim_incompatible_without_breaking() -> None:
    base = _cfg("base", {"script": [("a", ("x", "y")), ("b", ("m", "n"))]})
    cand = _cfg("cand", {"script": [("a", ("x", "y"))]})
    result = compare_tag_sets(base, cand, mode="BACKWARD", allow_breaking=False)
    assert result.compatible is False
    assert any(i.kind == "remove_dim" and i.dim == "b" for i in result.issues)


def test_relabel_detected() -> None:
    base = _cfg("base", {"plot_unit": [("a", ("x", "y"))]})
    cand = _cfg("cand", {"plot_unit": [("a", ("x", "z"))]})
    result = compare_tag_sets(base, cand, mode="BACKWARD", allow_breaking=False)
    assert any(i.kind == "relabel" and i.value_before == "y" and i.value_after == "z" for i in result.issues)


def test_parent_change_detected() -> None:
    base = _cfg("base", {"script": [("a", ("x", "y"))]})
    cand = _cfg("cand", {"episode": [("a", ("x", "y"))]})
    result = compare_tag_sets(base, cand, mode="BACKWARD", allow_breaking=False)
    assert any(i.kind == "parent_change" and i.dim == "a" for i in result.issues)


def test_breaking_flag_allows_incompatible_changes() -> None:
    base = _cfg("base", {"script": [("a", ("x", "y")), ("b", ("m", "n"))]})
    cand = _cfg("cand", {"script": [("a", ("x", "y"))]})
    result = compare_tag_sets(base, cand, mode="BACKWARD", allow_breaking=True)
    assert result.compatible is True

