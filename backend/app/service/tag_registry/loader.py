from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


_REGISTRY_ROOT = Path(__file__).resolve().parent
_TAG_SET_DIR = _REGISTRY_ROOT / "tag_sets"


@dataclass(frozen=True)
class DimConfig:
    scope: str
    dim: str
    cardinality: str = "single"
    open_enum: bool = False
    stability_state: str = "experimental"
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class BundleConfig:
    id: str
    scope: str
    dims: tuple[str, ...]
    prompt: str
    output_mode: str = "single_call"
    rule_overrides: dict[str, str] | None = None


@dataclass(frozen=True)
class TagSetConfig:
    version: str
    description: str
    prompt_ver: str
    breaking: bool
    scope_to_dims: dict[str, tuple[DimConfig, ...]]
    bundles: tuple[BundleConfig, ...]
    dim_to_bundle_id: dict[str, str]

    @property
    def all_dims(self) -> list[str]:
        dims: list[str] = []
        for items in self.scope_to_dims.values():
            dims.extend(item.dim for item in items)
        return dims

    def get_dim(self, dim: str) -> DimConfig:
        for items in self.scope_to_dims.values():
            for item in items:
                if item.dim == dim:
                    return item
        raise KeyError(f"dim {dim!r} not found in tag_set={self.version}")

    def get_bundle(self, bundle_id: str) -> BundleConfig:
        for bundle in self.bundles:
            if bundle.id == bundle_id:
                return bundle
        raise KeyError(f"bundle {bundle_id!r} not found in tag_set={self.version}")

    def list_bundles(self, scope: str | None = None) -> list[BundleConfig]:
        if scope is None:
            return list(self.bundles)
        return [b for b in self.bundles if b.scope == scope]

    def find_bundle_for_dim(self, dim: str) -> BundleConfig:
        bundle_id = self.dim_to_bundle_id.get(dim)
        if not bundle_id:
            raise KeyError(f"bundle mapping not found for dim={dim!r} in tag_set={self.version}")
        return self.get_bundle(bundle_id)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError as e:  # pragma: no cover - env/bootstrap error
        raise RuntimeError("pyyaml is required for tag_registry loader") from e

    if not path.exists():
        raise FileNotFoundError(f"tag set file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"invalid tag set yaml root: {path}")
    return data


def _merge_scope(base_scope: dict[str, list[dict[str, Any]]], cur_scope: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    merged = {scope: [dict(x) for x in dims] for scope, dims in base_scope.items()}
    for scope, dims in (cur_scope or {}).items():
        if not isinstance(dims, list):
            raise ValueError(f"scope[{scope}] must be list")
        existing = {d.get("dim"): i for i, d in enumerate(merged.get(scope, []))}
        out = merged.get(scope, [])
        for dim_cfg in dims:
            if not isinstance(dim_cfg, dict) or "dim" not in dim_cfg:
                raise ValueError(f"invalid dim config under scope={scope}: {dim_cfg!r}")
            key = dim_cfg["dim"]
            if key in existing:
                out[existing[key]] = dict(dim_cfg)
            else:
                out.append(dict(dim_cfg))
        merged[scope] = out
    return merged


def _merge_bundles(base: list[dict[str, Any]], cur: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = [dict(x) for x in base]
    by_id = {str(item.get("id")): idx for idx, item in enumerate(merged) if item.get("id")}
    for item in cur:
        if not isinstance(item, dict):
            raise ValueError(f"invalid bundle config: {item!r}")
        bid = str(item.get("id") or "").strip()
        if not bid:
            raise ValueError(f"bundle.id is required: {item!r}")
        if bid in by_id:
            merged[by_id[bid]] = dict(item)
        else:
            by_id[bid] = len(merged)
            merged.append(dict(item))
    return merged


def _load_raw_tag_set(version: str, seen: set[str] | None = None) -> dict[str, Any]:
    seen = seen or set()
    if version in seen:
        raise ValueError(f"cyclic tag set extends: {version}")
    seen.add(version)

    cur = _read_yaml(_TAG_SET_DIR / f"{version.split('.')[0]}.yaml")
    cur_ver = str(cur.get("version") or "").strip()
    if cur_ver != version:
        raise ValueError(f"tag set version mismatch: file={cur_ver!r} expected={version!r}")

    base_scope: dict[str, list[dict[str, Any]]] = {}
    base_bundles: list[dict[str, Any]] = []
    base_desc = ""
    base_prompt_ver = ""
    base_breaking = False

    extends = cur.get("extends")
    if extends:
        base = _load_raw_tag_set(str(extends), seen)
        base_scope = base["scope"]
        base_bundles = base.get("bundles", [])
        base_desc = base.get("description", "")
        base_prompt_ver = base.get("prompt_ver", "")
        base_breaking = bool(base.get("breaking", False))

    merged_scope = _merge_scope(base_scope, cur.get("scope") or {})
    merged_bundles = _merge_bundles(base_bundles, list(cur.get("bundles") or []))
    return {
        "version": version,
        "description": cur.get("description") or base_desc or "",
        "prompt_ver": cur.get("prompt_ver") or base_prompt_ver or version,
        "breaking": bool(cur.get("breaking", base_breaking)),
        "scope": merged_scope,
        "bundles": merged_bundles,
    }


@lru_cache(maxsize=8)
def load_tag_set(tag_set_ver: str) -> TagSetConfig:
    raw = _load_raw_tag_set(tag_set_ver)
    scope_to_dims: dict[str, tuple[DimConfig, ...]] = {}
    for scope, dims in raw["scope"].items():
        items: list[DimConfig] = []
        for dim_cfg in dims:
            values = tuple(str(v) for v in (dim_cfg.get("values") or []))
            items.append(
                DimConfig(
                    scope=scope,
                    dim=str(dim_cfg["dim"]),
                    cardinality=str(dim_cfg.get("cardinality") or "single"),
                    open_enum=bool(dim_cfg.get("open_enum", False)),
                    stability_state=str(dim_cfg.get("stability_state") or "experimental"),
                    values=values,
                )
            )
        scope_to_dims[scope] = tuple(items)

    bundles: list[BundleConfig] = []
    dim_to_bundle_id: dict[str, str] = {}
    for bundle_cfg in raw.get("bundles") or []:
        scope = str(bundle_cfg.get("scope") or "").strip()
        if scope not in scope_to_dims:
            raise ValueError(f"bundle scope={scope!r} not found in tag_set={tag_set_ver}")
        bundle_id = str(bundle_cfg.get("id") or "").strip()
        if not bundle_id:
            raise ValueError(f"bundle id is required in tag_set={tag_set_ver}")
        dims = tuple(str(x).strip() for x in (bundle_cfg.get("dims") or []) if str(x).strip())
        if not dims:
            raise ValueError(f"bundle {bundle_id!r} has empty dims in tag_set={tag_set_ver}")
        known_dims = {d.dim for d in scope_to_dims[scope]}
        unknown = [d for d in dims if d not in known_dims]
        if unknown:
            raise ValueError(
                f"bundle {bundle_id!r} has unknown dims for scope={scope!r}: {unknown}"
            )
        for dim in dims:
            if dim in dim_to_bundle_id and dim_to_bundle_id[dim] != bundle_id:
                raise ValueError(
                    f"dim {dim!r} is mapped to multiple bundles: "
                    f"{dim_to_bundle_id[dim]!r} and {bundle_id!r}"
                )
            dim_to_bundle_id[dim] = bundle_id
        rule_overrides = bundle_cfg.get("rule_overrides")
        parsed_rule_overrides: dict[str, str] | None = None
        if isinstance(rule_overrides, dict):
            parsed_rule_overrides = {
                str(k): str(v) for k, v in rule_overrides.items() if str(k).strip() and str(v).strip()
            }
        bundles.append(
            BundleConfig(
                id=bundle_id,
                scope=scope,
                dims=dims,
                prompt=str(bundle_cfg.get("prompt") or ""),
                output_mode=str(bundle_cfg.get("output_mode") or "single_call"),
                rule_overrides=parsed_rule_overrides,
            )
        )
    for dim in [d for items in scope_to_dims.values() for d in items]:
        if dim.dim not in dim_to_bundle_id:
            raise ValueError(f"dim {dim.dim!r} does not belong to any bundle in tag_set={tag_set_ver}")

    return TagSetConfig(
        version=str(raw["version"]),
        description=str(raw.get("description") or ""),
        prompt_ver=str(raw.get("prompt_ver") or raw["version"]),
        breaking=bool(raw.get("breaking", False)),
        scope_to_dims=scope_to_dims,
        bundles=tuple(bundles),
        dim_to_bundle_id=dim_to_bundle_id,
    )


def _resolve_prompt_path(path_like: str) -> Path:
    path = Path(path_like)
    if not path.is_absolute():
        path = _REGISTRY_ROOT / path
    return path


def load_prompt(tag_set_ver: str, dim: str) -> str:
    cfg = load_tag_set(tag_set_ver)
    cfg.get_dim(dim)
    bundle = cfg.find_bundle_for_dim(dim)
    path = _resolve_prompt_path(bundle.prompt)
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def load_prompt_by_bundle(tag_set_ver: str, bundle_id: str) -> str:
    cfg = load_tag_set(tag_set_ver)
    bundle = cfg.get_bundle(bundle_id)
    path = _resolve_prompt_path(bundle.prompt)
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def load_bundle(tag_set_ver: str, bundle_id: str) -> BundleConfig:
    cfg = load_tag_set(tag_set_ver)
    return cfg.get_bundle(bundle_id)


def list_bundles(tag_set_ver: str, scope: str | None = None) -> list[BundleConfig]:
    cfg = load_tag_set(tag_set_ver)
    return cfg.list_bundles(scope=scope)


def get_prompt_ver(tag_set_ver: str, dim: str, variant: str = "a") -> str:
    cfg = load_tag_set(tag_set_ver)
    cfg.get_dim(dim)
    return f"{cfg.prompt_ver}:{dim}:{variant}"
