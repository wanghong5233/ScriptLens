from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.score_registry import RubricConfig
from utils.database import engine as default_engine


@dataclass
class SignalValue:
    key: str
    value: Any
    score: float | None
    source: str
    confidence: float = 0.7
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    primary_dimension: str | None = None
    weight_in_dim: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "score": self.score,
            "source": self.source,
            "confidence": self.confidence,
            "evidence_refs": self.evidence_refs,
            "primary_dimension": self.primary_dimension,
            "weight_in_dim": self.weight_in_dim,
            "meta": self.meta,
        }


@dataclass
class SignalContext:
    script_id: str
    script_meta: dict[str, Any]
    plot_units: list[dict[str, Any]]
    plot_unit_tags: list[dict[str, Any]]
    script_tags: list[dict[str, Any]]
    episode_tags: list[dict[str, Any]]
    character_entities: list[dict[str, Any]]
    character_relationships: list[dict[str, Any]]
    scenes: list[dict[str, Any]]
    drama_tags: list[str]
    plot_tags_by_unit: dict[str, dict[str, list[str]]]
    script_tag_map: dict[str, list[str]]
    episode_tag_map: dict[int, dict[str, list[str]]]
    tag_set_ver: str = "v1.0.0"

    @property
    def episode_count(self) -> int:
        if self.script_meta.get("total_episodes"):
            return int(self.script_meta["total_episodes"])
        if self.plot_units:
            eps = {int(unit["episode_no"]) for unit in self.plot_units if unit.get("episode_no") is not None}
            if eps:
                return len(eps)
        return 1

    @property
    def plot_unit_count(self) -> int:
        return len(self.plot_units)

    def unit_value(self, plot_unit_id: str, dim: str, default: str = "none") -> str:
        values = self.plot_tags_by_unit.get(plot_unit_id, {}).get(dim, [])
        if not values:
            return default
        return str(values[0]).strip() or default

    def plot_values(self, dim: str) -> list[str]:
        out: list[str] = []
        for row in self.plot_unit_tags:
            if row.get("dim") != dim:
                continue
            value = str(row.get("value") or "").strip()
            if value:
                out.append(value)
        return out

    def script_values(self, dim: str) -> list[str]:
        return list(self.script_tag_map.get(dim, []))

    def episode_values(self, dim: str) -> list[str]:
        out: list[str] = []
        for payload in self.episode_tag_map.values():
            out.extend(payload.get(dim, []))
        return out


@dataclass(frozen=True)
class _SignalSpec:
    key: str
    scope: str
    source: str
    primary_dim: str | None
    func: Callable[[SignalContext], Any]


_SIGNAL_REGISTRY: dict[str, _SignalSpec] = {}


def register_signal(
    key: str,
    *,
    scope: str,
    source: str,
    primary_dim: str | None = None,
) -> Callable[[Callable[[SignalContext], Any]], Callable[[SignalContext], Any]]:
    def decorator(func: Callable[[SignalContext], Any]) -> Callable[[SignalContext], Any]:
        normalized_key = str(key).strip()
        if not normalized_key:
            raise ValueError("signal key must not be empty")
        _SIGNAL_REGISTRY[normalized_key] = _SignalSpec(
            key=normalized_key,
            scope=str(scope).strip() or "script",
            source=str(source).strip() or "rule",
            primary_dim=primary_dim,
            func=func,
        )
        return func

    return decorator


def _to_signal_value(spec: _SignalSpec, raw: Any) -> SignalValue:
    if isinstance(raw, SignalValue):
        return SignalValue(
            key=spec.key,
            value=raw.value,
            score=raw.score,
            source=raw.source or spec.source,
            confidence=raw.confidence,
            evidence_refs=list(raw.evidence_refs or []),
            primary_dimension=raw.primary_dimension or spec.primary_dim,
            weight_in_dim=raw.weight_in_dim,
            meta=dict(raw.meta or {}),
        )
    if isinstance(raw, bool):
        score = 10.0 if raw else 0.0
        return SignalValue(key=spec.key, value=raw, score=score, source=spec.source, primary_dimension=spec.primary_dim)
    if isinstance(raw, (int, float)):
        return SignalValue(
            key=spec.key,
            value=float(raw),
            score=float(raw),
            source=spec.source,
            primary_dimension=spec.primary_dim,
        )
    if isinstance(raw, dict):
        return SignalValue(
            key=spec.key,
            value=raw.get("value"),
            score=float(raw["score"]) if raw.get("score") is not None else None,
            source=str(raw.get("source") or spec.source),
            confidence=float(raw.get("confidence") or 0.7),
            evidence_refs=list(raw.get("evidence_refs") or []),
            primary_dimension=str(raw.get("primary_dimension") or spec.primary_dim or ""),
            weight_in_dim=float(raw["weight_in_dim"]) if raw.get("weight_in_dim") is not None else None,
            meta=dict(raw.get("meta") or {}),
        )
    return SignalValue(
        key=spec.key,
        value=raw,
        score=None,
        source=spec.source,
        primary_dimension=spec.primary_dim,
        confidence=0.0,
    )


def build_signal_context(*, script_id: str, engine: Engine = default_engine) -> SignalContext:
    with engine.connect() as conn:
        script_meta_row = conn.execute(
            text(
                """
                SELECT id::text AS id,
                       title,
                       COALESCE(total_episodes, 0) AS total_episodes,
                       COALESCE(total_scenes, 0) AS total_scenes
                FROM scriptlens.scripts
                WHERE id = :sid
                """
            ),
            {"sid": script_id},
        ).mappings().first()
        if script_meta_row is None:
            raise ValueError(f"script_id={script_id} not found")
        script_meta = dict(script_meta_row)

        plot_units = [
            dict(row)
            for row in conn.execute(
                text(
                    """
                    SELECT id::text AS id, episode_no, idx, summary, start_line, end_line
                    FROM scriptlens.plot_units
                    WHERE script_id = :sid
                    ORDER BY idx
                    """
                ),
                {"sid": script_id},
            ).mappings().all()
        ]

        plot_unit_tags = [
            dict(row)
            for row in conn.execute(
                text(
                    """
                    SELECT t.plot_unit_id::text AS plot_unit_id,
                           t.dim,
                           t.value,
                           t.score,
                           t.confidence
                    FROM scriptlens.plot_unit_tags t
                    JOIN scriptlens.plot_units pu ON pu.id = t.plot_unit_id
                    WHERE pu.script_id = :sid
                    """
                ),
                {"sid": script_id},
            ).mappings().all()
        ]

        script_tags = [
            dict(row)
            for row in conn.execute(
                text(
                    """
                    SELECT dim, value, score, confidence
                    FROM scriptlens.script_tags
                    WHERE script_id = :sid
                    """
                ),
                {"sid": script_id},
            ).mappings().all()
        ]

        episode_tags = [
            dict(row)
            for row in conn.execute(
                text(
                    """
                    SELECT episode_no, dim, value, score, confidence
                    FROM scriptlens.episode_tags
                    WHERE script_id = :sid
                    """
                ),
                {"sid": script_id},
            ).mappings().all()
        ]

        character_entities = [
            dict(row)
            for row in conn.execute(
                text(
                    """
                    SELECT id::text AS id,
                           canonical_name,
                           role,
                           arc_type,
                           agency_level
                    FROM scriptlens.character_entities
                    WHERE script_id = :sid
                    """
                ),
                {"sid": script_id},
            ).mappings().all()
        ]

        character_relationships = [
            dict(row)
            for row in conn.execute(
                text(
                    """
                    SELECT id::text AS id,
                           src_char_id::text AS src_char_id,
                           dst_char_id::text AS dst_char_id,
                           relationship_type,
                           polarity,
                           dynamic_arc,
                           triangle
                    FROM scriptlens.character_relationships
                    WHERE script_id = :sid
                    """
                ),
                {"sid": script_id},
            ).mappings().all()
        ]

        scenes = [
            dict(row)
            for row in conn.execute(
                text(
                    """
                    SELECT id::text AS id, episode_no, scene_no, text
                    FROM scriptlens.scenes
                    WHERE script_id = :sid
                    ORDER BY episode_no NULLS LAST, scene_no
                    """
                ),
                {"sid": script_id},
            ).mappings().all()
        ]

    plot_tags_by_unit: dict[str, dict[str, list[str]]] = {}
    for row in plot_unit_tags:
        unit_id = str(row.get("plot_unit_id") or "").strip()
        if not unit_id:
            continue
        dim = str(row.get("dim") or "").strip()
        value = str(row.get("value") or "").strip()
        if not dim or not value:
            continue
        per_dim = plot_tags_by_unit.setdefault(unit_id, {})
        values = per_dim.setdefault(dim, [])
        values.append(value)

    script_tag_map: dict[str, list[str]] = {}
    for row in script_tags:
        dim = str(row.get("dim") or "").strip()
        value = str(row.get("value") or "").strip()
        if not dim or not value:
            continue
        script_tag_map.setdefault(dim, []).append(value)

    episode_tag_map: dict[int, dict[str, list[str]]] = {}
    for row in episode_tags:
        episode_no = row.get("episode_no")
        if episode_no is None:
            continue
        dim = str(row.get("dim") or "").strip()
        value = str(row.get("value") or "").strip()
        if not dim or not value:
            continue
        payload = episode_tag_map.setdefault(int(episode_no), {})
        payload.setdefault(dim, []).append(value)

    drama_tags = [tag for tag in script_tag_map.get("drama_tags", []) if tag]
    return SignalContext(
        script_id=script_id,
        script_meta=script_meta,
        plot_units=plot_units,
        plot_unit_tags=plot_unit_tags,
        script_tags=script_tags,
        episode_tags=episode_tags,
        character_entities=character_entities,
        character_relationships=character_relationships,
        scenes=scenes,
        drama_tags=drama_tags,
        plot_tags_by_unit=plot_tags_by_unit,
        script_tag_map=script_tag_map,
        episode_tag_map=episode_tag_map,
    )


def compute_rule_signals(rubric: RubricConfig, ctx: SignalContext) -> dict[str, SignalValue]:
    # signal metadata from rubric, used to decorate outputs.
    rubric_signal_meta: dict[str, tuple[str | None, float | None]] = {}
    for dim in rubric.dimensions:
        for signal in dim.signals:
            if signal.primary or signal.id not in rubric_signal_meta:
                rubric_signal_meta[signal.id] = (dim.id if signal.primary else None, signal.weight_in_dim)

    out: dict[str, SignalValue] = {}
    for signal in rubric.list_signals():
        if signal.source not in {"rule", "hybrid"}:
            continue
        spec = _SIGNAL_REGISTRY.get(signal.id)
        if spec is None:
            out[signal.id] = SignalValue(
                key=signal.id,
                value=None,
                score=None,
                source="rule",
                confidence=0.0,
                primary_dimension=rubric_signal_meta.get(signal.id, (None, None))[0],
                weight_in_dim=signal.weight_in_dim,
                meta={"missing_impl": True},
            )
            continue
        try:
            resolved = _to_signal_value(spec, spec.func(ctx))
        except Exception as exc:  # noqa: BLE001
            resolved = SignalValue(
                key=signal.id,
                value=None,
                score=None,
                source=spec.source,
                confidence=0.0,
                primary_dimension=spec.primary_dim,
                weight_in_dim=signal.weight_in_dim,
                meta={"error": f"{type(exc).__name__}: {exc}"},
            )
        if resolved.weight_in_dim is None:
            resolved.weight_in_dim = signal.weight_in_dim
        if not resolved.primary_dimension:
            resolved.primary_dimension = rubric_signal_meta.get(signal.id, (None, None))[0]
        out[signal.id] = resolved
    return out


async def compute_signals(
    rubric: RubricConfig,
    ctx: SignalContext,
    *,
    caller: Any | None = None,
    seed: int = 42,
) -> dict[str, SignalValue]:
    out = compute_rule_signals(rubric, ctx)

    # LLM signals are added in phase 2b. Keep import lazy to avoid cycles when rule-only tests run.
    from service.script_tools.signal_catalog.llm_signals import compute_llm_signals

    llm_values = await compute_llm_signals(rubric, ctx, caller=caller, seed=seed)
    out.update(llm_values)
    return out


def get_registered_signals() -> list[str]:
    return sorted(_SIGNAL_REGISTRY.keys())


# rule signal registration side effects
from service.script_tools.signal_catalog.rule_signals import character as _character  # noqa: E402,F401
from service.script_tools.signal_catalog.rule_signals import concept as _concept  # noqa: E402,F401
from service.script_tools.signal_catalog.rule_signals import dialogue as _dialogue  # noqa: E402,F401
from service.script_tools.signal_catalog.rule_signals import emotion as _emotion  # noqa: E402,F401
from service.script_tools.signal_catalog.rule_signals import pacing as _pacing  # noqa: E402,F401
from service.script_tools.signal_catalog.rule_signals import story as _story  # noqa: E402,F401
