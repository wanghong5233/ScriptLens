from __future__ import annotations

from typing import Any

from service.tag_registry.loader import load_tag_set


class TagValueError(ValueError):
    pass


def _validate_single(dim: str, value: Any, *, allowed: set[str], open_enum: bool) -> None:
    if not isinstance(value, str):
        raise TagValueError(f"dim={dim} expects single string, got {type(value).__name__}")
    v = value.strip()
    if not v:
        raise TagValueError(f"dim={dim} expects non-empty string")
    if open_enum:
        return
    if v not in allowed:
        raise TagValueError(f"dim={dim} value={v!r} not in enum")


def _validate_multi(dim: str, value: Any, *, allowed: set[str], open_enum: bool) -> None:
    if not isinstance(value, list):
        raise TagValueError(f"dim={dim} expects list[str], got {type(value).__name__}")
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise TagValueError(f"dim={dim}[{idx}] expects string, got {type(item).__name__}")
        item = item.strip()
        if not item:
            raise TagValueError(f"dim={dim}[{idx}] expects non-empty string")
        if not open_enum and item not in allowed:
            raise TagValueError(f"dim={dim}[{idx}] value={item!r} not in enum")


def validate(tag_set_ver: str, dim: str, value: Any) -> None:
    cfg = load_tag_set(tag_set_ver)
    dim_cfg = cfg.get_dim(dim)
    allowed = set(dim_cfg.values)
    if dim_cfg.cardinality == "multi":
        _validate_multi(dim, value, allowed=allowed, open_enum=dim_cfg.open_enum)
        return
    _validate_single(dim, value, allowed=allowed, open_enum=dim_cfg.open_enum)


def validate_tagset(
    tag_set_ver: str,
    tagset: dict[str, Any],
    *,
    dims: list[str] | None = None,
    allow_partial: bool = False,
) -> dict[str, str]:
    cfg = load_tag_set(tag_set_ver)
    if not isinstance(tagset, dict):
        raise TagValueError(f"tagset must be dict, got {type(tagset).__name__}")

    target_dims = dims or cfg.all_dims
    errors: dict[str, str] = {}

    # Unknown dims in payload
    for dim in tagset.keys():
        try:
            cfg.get_dim(dim)
        except KeyError:
            errors[dim] = f"unknown dim for tag_set={tag_set_ver}"

    for dim in target_dims:
        if dim not in tagset:
            if not allow_partial:
                errors[dim] = "missing dim"
            continue
        try:
            validate(tag_set_ver, dim, tagset[dim])
        except TagValueError as e:
            errors[dim] = str(e)
    return errors
