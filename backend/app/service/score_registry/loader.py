# ============================================================
# DEPRECATED — release/v1-mvp (2026-05-29)
# ============================================================
#
# 本文件属于已废弃的「整剧抽情节打标签 → rubric/signal/aggregator
# 评分」流水线（Batch3 体系）。release/v1-mvp 已切回 self-contained
# 6 维规则评分，主流程入口：
#   - service/script_tools/dimension_scorer.py
#   - service/script_report_service.py（generate_report）
# 当前已不再调用本模块任何函数。
#
# 保留原因：避免 git history 大面积污染、便于必要时回收实现细节。
# 清理时机：下次 cleanup PR 统一删除（含本文件、其测试、CLI 入口
# 与 score_registry/rubric_sets/v3.yaml 等配套资产）。
#
# 不要在本文件内再做任何功能性修改。如需新评分能力，请扩展
# dimension_scorer.py。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


_REGISTRY_ROOT = Path(__file__).resolve().parent
_RUBRIC_SET_DIR = _REGISTRY_ROOT / "rubric_sets"


@dataclass(frozen=True)
class SignalConfig:
    id: str
    weight_in_dim: float
    source: str = "rule"
    primary: bool = False


@dataclass(frozen=True)
class DimensionConfig:
    id: str
    signals: tuple[SignalConfig, ...]


@dataclass(frozen=True)
class LlmBundleConfig:
    id: str
    scope: str
    signals: tuple[str, ...]
    prompt: str


@dataclass(frozen=True)
class RubricConfig:
    rubric_id: str
    status: str
    description: str
    score_ver: str
    breaking: bool
    base_weight: dict[str, float]
    genre_multiplier: dict[str, dict[str, float]]
    tier_cuts: dict[str, dict[str, dict[str, float]]]
    dimensions: tuple[DimensionConfig, ...]
    llm_bundles: tuple[LlmBundleConfig, ...]

    @property
    def all_dimensions(self) -> list[str]:
        return [dim.id for dim in self.dimensions]

    @property
    def all_signals(self) -> list[str]:
        out: list[str] = []
        for dim in self.dimensions:
            for signal in dim.signals:
                if signal.id not in out:
                    out.append(signal.id)
        return out

    def get_dimension(self, dimension_id: str) -> DimensionConfig:
        for dim in self.dimensions:
            if dim.id == dimension_id:
                return dim
        raise KeyError(f"dimension {dimension_id!r} not found in rubric={self.rubric_id}")

    def get_bundle(self, bundle_id: str) -> LlmBundleConfig:
        for bundle in self.llm_bundles:
            if bundle.id == bundle_id:
                return bundle
        raise KeyError(f"llm bundle {bundle_id!r} not found in rubric={self.rubric_id}")

    def list_llm_bundles(self, scope: str | None = None) -> list[LlmBundleConfig]:
        if scope is None:
            return list(self.llm_bundles)
        return [bundle for bundle in self.llm_bundles if bundle.scope == scope]

    def list_signals(self, dimension_id: str | None = None) -> list[SignalConfig]:
        if dimension_id is None:
            seen: set[str] = set()
            out: list[SignalConfig] = []
            for dim in self.dimensions:
                for signal in dim.signals:
                    if signal.id in seen:
                        continue
                    seen.add(signal.id)
                    out.append(signal)
            return out
        return list(self.get_dimension(dimension_id).signals)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - env/bootstrap issue
        raise RuntimeError("pyyaml is required for score_registry loader") from exc

    if not path.exists():
        raise FileNotFoundError(f"rubric file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"invalid rubric yaml root: {path}")
    return data


def _merge_dimensions(
    base_dims: list[dict[str, Any]],
    cur_dims: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = [dict(item) for item in base_dims]
    by_id = {str(item.get("id")): idx for idx, item in enumerate(merged) if item.get("id")}
    for item in cur_dims:
        if not isinstance(item, dict):
            raise ValueError(f"invalid dimension config: {item!r}")
        dim_id = str(item.get("id") or "").strip()
        if not dim_id:
            raise ValueError(f"dimension.id is required: {item!r}")
        if dim_id in by_id:
            merged[by_id[dim_id]] = dict(item)
        else:
            by_id[dim_id] = len(merged)
            merged.append(dict(item))
    return merged


def _merge_llm_bundles(
    base_bundles: list[dict[str, Any]],
    cur_bundles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = [dict(item) for item in base_bundles]
    by_id = {str(item.get("id")): idx for idx, item in enumerate(merged) if item.get("id")}
    for item in cur_bundles:
        if not isinstance(item, dict):
            raise ValueError(f"invalid llm bundle config: {item!r}")
        bundle_id = str(item.get("id") or "").strip()
        if not bundle_id:
            raise ValueError(f"llm bundle.id is required: {item!r}")
        if bundle_id in by_id:
            merged[by_id[bundle_id]] = dict(item)
        else:
            by_id[bundle_id] = len(merged)
            merged.append(dict(item))
    return merged


def _merge_nested_dict(
    base: dict[str, Any],
    cur: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {k: dict(v) if isinstance(v, dict) else v for k, v in (base or {}).items()}
    for key, value in (cur or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def _rubric_file_for(rubric_id: str) -> Path:
    file_stem = rubric_id.split(".")[0]
    return _RUBRIC_SET_DIR / f"{file_stem}.yaml"


def _load_raw_rubric(rubric_id: str, seen: set[str] | None = None) -> dict[str, Any]:
    seen = seen or set()
    if rubric_id in seen:
        raise ValueError(f"cyclic rubric extends: {rubric_id}")
    seen.add(rubric_id)

    cur = _read_yaml(_rubric_file_for(rubric_id))
    cur_id = str(cur.get("rubric_id") or "").strip()
    if cur_id != rubric_id:
        raise ValueError(f"rubric id mismatch: file={cur_id!r} expected={rubric_id!r}")

    base_dims: list[dict[str, Any]] = []
    base_bundles: list[dict[str, Any]] = []
    base_weight: dict[str, float] = {}
    genre_multiplier: dict[str, Any] = {}
    tier_cuts: dict[str, Any] = {}
    base_description = ""
    base_status = "experimental"
    base_score_ver = rubric_id
    base_breaking = False

    extends = cur.get("extends")
    if extends:
        parent = _load_raw_rubric(str(extends), seen=seen)
        base_dims = [dict(item) for item in (parent.get("dimensions") or [])]
        base_bundles = [dict(item) for item in (parent.get("llm_bundles") or [])]
        base_weight = dict(parent.get("base_weight") or {})
        genre_multiplier = dict(parent.get("genre_multiplier") or {})
        tier_cuts = dict(parent.get("tier_cuts") or {})
        base_description = str(parent.get("description") or "")
        base_status = str(parent.get("status") or "experimental")
        base_score_ver = str(parent.get("score_ver") or parent["rubric_id"])
        base_breaking = bool(parent.get("breaking", False))

    merged = {
        "rubric_id": rubric_id,
        "status": str(cur.get("status") or base_status),
        "description": str(cur.get("description") or base_description),
        "score_ver": str(cur.get("score_ver") or base_score_ver or rubric_id),
        "breaking": bool(cur.get("breaking", base_breaking)),
        "base_weight": {**base_weight, **(cur.get("base_weight") or {})},
        "genre_multiplier": _merge_nested_dict(genre_multiplier, cur.get("genre_multiplier") or {}),
        "tier_cuts": _merge_nested_dict(tier_cuts, cur.get("tier_cuts") or {}),
        "dimensions": _merge_dimensions(base_dims, list(cur.get("dimensions") or [])),
        "llm_bundles": _merge_llm_bundles(base_bundles, list(cur.get("llm_bundles") or [])),
    }
    return merged


def _parse_signal(signal_cfg: Any) -> SignalConfig:
    if isinstance(signal_cfg, str):
        return SignalConfig(id=signal_cfg.strip(), weight_in_dim=1.0, source="rule", primary=False)
    if not isinstance(signal_cfg, dict):
        raise ValueError(f"invalid signal config: {signal_cfg!r}")
    signal_id = str(signal_cfg.get("id") or "").strip()
    if not signal_id:
        raise ValueError(f"signal.id is required: {signal_cfg!r}")
    raw_weight = signal_cfg.get("weight_in_dim", 1.0)
    try:
        weight_in_dim = float(raw_weight)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid weight_in_dim for signal={signal_id}: {raw_weight!r}") from exc
    if weight_in_dim <= 0:
        raise ValueError(f"weight_in_dim must be > 0 for signal={signal_id}")
    source = str(signal_cfg.get("source") or "rule").strip().lower()
    if source not in {"rule", "llm", "hybrid"}:
        raise ValueError(f"invalid source={source!r} for signal={signal_id}")
    primary = bool(signal_cfg.get("primary", False))
    return SignalConfig(
        id=signal_id,
        weight_in_dim=weight_in_dim,
        source=source,
        primary=primary,
    )


@lru_cache(maxsize=8)
def load_rubric(rubric_id: str) -> RubricConfig:
    raw = _load_raw_rubric(rubric_id)
    dimensions: list[DimensionConfig] = []
    dim_ids: list[str] = []
    signal_ids: set[str] = set()
    for dim_cfg in raw.get("dimensions") or []:
        if not isinstance(dim_cfg, dict):
            raise ValueError(f"invalid dimension config: {dim_cfg!r}")
        dim_id = str(dim_cfg.get("id") or "").strip()
        if not dim_id:
            raise ValueError(f"dimension.id is required: {dim_cfg!r}")
        if dim_id in dim_ids:
            raise ValueError(f"duplicate dimension id={dim_id!r}")
        raw_signals = list(dim_cfg.get("signals") or [])
        if not raw_signals:
            raise ValueError(f"dimension={dim_id} has no signals")
        parsed_signals = tuple(_parse_signal(item) for item in raw_signals)
        local_seen: set[str] = set()
        for signal in parsed_signals:
            if signal.id in local_seen:
                raise ValueError(f"duplicate signal id={signal.id!r} in dimension={dim_id}")
            local_seen.add(signal.id)
            signal_ids.add(signal.id)
        dimensions.append(DimensionConfig(id=dim_id, signals=parsed_signals))
        dim_ids.append(dim_id)

    base_weight_raw = raw.get("base_weight") or {}
    base_weight: dict[str, float] = {}
    for dim_id in dim_ids:
        raw_weight = base_weight_raw.get(dim_id, 1.0)
        try:
            value = float(raw_weight)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid base_weight for dimension={dim_id}: {raw_weight!r}") from exc
        if value <= 0:
            raise ValueError(f"base_weight must be > 0 for dimension={dim_id}")
        base_weight[dim_id] = value

    genre_multiplier_raw = raw.get("genre_multiplier") or {}
    genre_multiplier: dict[str, dict[str, float]] = {}
    for genre, weights in genre_multiplier_raw.items():
        if not isinstance(weights, dict):
            continue
        parsed_weights: dict[str, float] = {}
        for dim_id in dim_ids:
            raw_weight = weights.get(dim_id, 1.0)
            try:
                parsed_weights[dim_id] = float(raw_weight)
            except (TypeError, ValueError):
                parsed_weights[dim_id] = 1.0
        genre_multiplier[str(genre)] = parsed_weights
    if "default" not in genre_multiplier:
        genre_multiplier["default"] = {dim_id: 1.0 for dim_id in dim_ids}

    tier_cuts_raw = raw.get("tier_cuts") or {}
    tier_cuts: dict[str, dict[str, dict[str, float]]] = {}
    for genre, dim_cut_map in tier_cuts_raw.items():
        if not isinstance(dim_cut_map, dict):
            continue
        parsed_dim_cut_map: dict[str, dict[str, float]] = {}
        for dim_id in dim_ids:
            cuts = dim_cut_map.get(dim_id) or {}
            if not isinstance(cuts, dict):
                cuts = {}
            p25 = float(cuts.get("p25", 4.0))
            p50 = float(cuts.get("p50", 6.0))
            p75 = float(cuts.get("p75", 8.0))
            parsed_dim_cut_map[dim_id] = {"p25": p25, "p50": p50, "p75": p75}
        tier_cuts[str(genre)] = parsed_dim_cut_map
    if "default" not in tier_cuts:
        tier_cuts["default"] = {
            dim_id: {"p25": 4.0, "p50": 6.0, "p75": 8.0} for dim_id in dim_ids
        }

    llm_bundles: list[LlmBundleConfig] = []
    for bundle_cfg in raw.get("llm_bundles") or []:
        if not isinstance(bundle_cfg, dict):
            raise ValueError(f"invalid llm bundle config: {bundle_cfg!r}")
        bundle_id = str(bundle_cfg.get("id") or "").strip()
        scope = str(bundle_cfg.get("scope") or "").strip()
        prompt = str(bundle_cfg.get("prompt") or "").strip()
        signals = tuple(str(item).strip() for item in (bundle_cfg.get("signals") or []) if str(item).strip())
        if not bundle_id:
            raise ValueError(f"llm bundle.id is required: {bundle_cfg!r}")
        if not scope:
            raise ValueError(f"llm bundle.scope is required: id={bundle_id}")
        if not prompt:
            raise ValueError(f"llm bundle.prompt is required: id={bundle_id}")
        if not signals:
            raise ValueError(f"llm bundle.signals is empty: id={bundle_id}")
        unknown = [signal for signal in signals if signal not in signal_ids]
        if unknown:
            raise ValueError(f"llm bundle id={bundle_id} has unknown signals: {unknown}")
        llm_bundles.append(
            LlmBundleConfig(
                id=bundle_id,
                scope=scope,
                signals=signals,
                prompt=prompt,
            )
        )

    return RubricConfig(
        rubric_id=str(raw["rubric_id"]),
        status=str(raw.get("status") or "experimental"),
        description=str(raw.get("description") or ""),
        score_ver=str(raw.get("score_ver") or raw["rubric_id"]),
        breaking=bool(raw.get("breaking", False)),
        base_weight=base_weight,
        genre_multiplier=genre_multiplier,
        tier_cuts=tier_cuts,
        dimensions=tuple(dimensions),
        llm_bundles=tuple(llm_bundles),
    )


def _resolve_prompt_path(path_like: str) -> Path:
    path = Path(path_like)
    if not path.is_absolute():
        path = _REGISTRY_ROOT / path
    return path


def load_prompt_by_bundle(rubric_id: str, bundle_id: str) -> str:
    rubric = load_rubric(rubric_id)
    bundle = rubric.get_bundle(bundle_id)
    path = _resolve_prompt_path(bundle.prompt)
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def list_signals(rubric_id: str, dimension_id: str | None = None) -> list[SignalConfig]:
    rubric = load_rubric(rubric_id)
    return rubric.list_signals(dimension_id=dimension_id)


def list_llm_bundles(rubric_id: str, scope: str | None = None) -> list[LlmBundleConfig]:
    rubric = load_rubric(rubric_id)
    return rubric.list_llm_bundles(scope=scope)


def get_genre_multiplier(rubric_id: str, genre: str | None = None) -> dict[str, float]:
    rubric = load_rubric(rubric_id)
    if genre and genre in rubric.genre_multiplier:
        return dict(rubric.genre_multiplier[genre])
    return dict(rubric.genre_multiplier.get("default", {}))


def get_tier_cuts(rubric_id: str, genre: str | None = None) -> dict[str, dict[str, float]]:
    rubric = load_rubric(rubric_id)
    if genre and genre in rubric.tier_cuts:
        return dict(rubric.tier_cuts[genre])
    return dict(rubric.tier_cuts.get("default", {}))
