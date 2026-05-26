from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


_REGISTRY_ROOT = Path(__file__).resolve().parent
_TAG_SET_DIR = _REGISTRY_ROOT / "tag_sets"
_PROMPT_DIR = _REGISTRY_ROOT / "prompts"


_ASR_DIMS = {
    "dialogue_density",
    "speech_style",
    "cta_type",
    "voiceover_type",
    "emotional_keywords",
    "keyword_theme",
}
_PLOT_DIMS = {
    "plot_hook",
    "conflict_type",
    "story_stage",
    "relationship_arc",
    "payoff_type",
    "emotional_driver",
    "business_content_archetype",
    "business_conflict_bucket",
    "business_payoff_bucket",
    "business_emotion_bucket",
}
_V1_SCRIPT_STRUCTURE_DIMS = {
    "gender_axis",
    "world_setting",
    "protagonist_archetype",
    "antagonist_archetype",
    "pacing_mode",
    "paid_break_pattern",
    "story_arc_template",
}
_V1_CHARACTER_DIMS = {
    "character_archetype",
    "character_role_in_arc",
    "character_arc_type",
    "character_agency_level",
}
_V1_RELATION_DIMS = {
    "relationship_type",
    "relationship_polarity",
    "relationship_dynamic_arc",
    "relationship_triangle",
}
_V1_EPISODE_DIMS = {
    "episode_opening_type",
    "episode_end_hook",
    "intra_episode_peak_count",
    "paid_break_position",
}
_V2_STORYBOARD_DIMS = {
    "scene_locale_type",
    "scene_time_of_day",
    "scene_in_out",
    "scene_emotion_keynote",
    "shot_suggestion",
    "prop_focus",
    "character_state_change",
}


@dataclass(frozen=True)
class DimConfig:
    scope: str
    dim: str
    cardinality: str = "single"
    open_enum: bool = False
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class TagSetConfig:
    version: str
    description: str
    prompt_ver: str
    scope_to_dims: dict[str, tuple[DimConfig, ...]]

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
    base_desc = ""
    base_prompt_ver = ""

    extends = cur.get("extends")
    if extends:
        base = _load_raw_tag_set(str(extends), seen)
        base_scope = base["scope"]
        base_desc = base.get("description", "")
        base_prompt_ver = base.get("prompt_ver", "")

    merged_scope = _merge_scope(base_scope, cur.get("scope") or {})
    return {
        "version": version,
        "description": cur.get("description") or base_desc or "",
        "prompt_ver": cur.get("prompt_ver") or base_prompt_ver or version,
        "scope": merged_scope,
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
                    values=values,
                )
            )
        scope_to_dims[scope] = tuple(items)
    return TagSetConfig(
        version=str(raw["version"]),
        description=str(raw.get("description") or ""),
        prompt_ver=str(raw.get("prompt_ver") or raw["version"]),
        scope_to_dims=scope_to_dims,
    )


def _resolve_prompt_file(dim: str) -> Path:
    if dim == "drama_tags":
        return _PROMPT_DIR / "v0" / "drama_tags.jinja"
    if dim in _ASR_DIMS:
        return _PROMPT_DIR / "v0" / "asr.jinja"
    if dim in _PLOT_DIMS:
        return _PROMPT_DIR / "v0" / "plot.jinja"
    if dim in _V1_SCRIPT_STRUCTURE_DIMS:
        return _PROMPT_DIR / "v1" / "script_structure.jinja"
    if dim in _V1_CHARACTER_DIMS:
        return _PROMPT_DIR / "v1" / "character_attrs.jinja"
    if dim in _V1_RELATION_DIMS:
        return _PROMPT_DIR / "v1" / "relationship.jinja"
    if dim in _V1_EPISODE_DIMS:
        return _PROMPT_DIR / "v1" / "episode_structure.jinja"
    if dim in _V2_STORYBOARD_DIMS:
        return _PROMPT_DIR / "v2" / "storyboard_hints.jinja"
    raise KeyError(f"prompt file mapping not found for dim={dim}")


def load_prompt(tag_set_ver: str, dim: str) -> str:
    # Ensure dim belongs to this tag set first.
    cfg = load_tag_set(tag_set_ver)
    cfg.get_dim(dim)
    path = _resolve_prompt_file(dim)
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def get_prompt_ver(tag_set_ver: str, dim: str, variant: str = "a") -> str:
    cfg = load_tag_set(tag_set_ver)
    cfg.get_dim(dim)
    return f"{cfg.prompt_ver}:{dim}:{variant}"
