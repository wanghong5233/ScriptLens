from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from service.script_tools.dimension_aggregator import DimensionScore
from service.script_tools.signal_catalog import SignalValue

_DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent / "improvement_templates"
_ACTIONABLE_TIERS = {"poor", "weak"}


@dataclass
class ImprovementAction:
    id: str
    run_id: str
    script_id: str
    dimension: str
    signal_key: str
    template_id: str
    issue: str
    target: str
    action_steps: list[str]
    evidence_refs: list[dict[str, Any]]
    estimated_lift: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "script_id": self.script_id,
            "dimension": self.dimension,
            "signal_key": self.signal_key,
            "template_id": self.template_id,
            "issue": self.issue,
            "target": self.target,
            "action_steps": list(self.action_steps),
            "evidence_refs": list(self.evidence_refs),
            "estimated_lift": dict(self.estimated_lift),
        }


class _SafeDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render_template(value: str, context: dict[str, Any]) -> str:
    if not value:
        return ""
    try:
        return str(value).format_map(_SafeDict(context)).strip()
    except Exception:
        return str(value).strip()


def _load_templates(template_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not template_dir.exists():
        return out
    for path in sorted(template_dir.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            continue
        signal_key = str(payload.get("signal_key") or path.stem).strip()
        if not signal_key:
            continue
        out[signal_key] = payload
    return out


def generate_actions(
    *,
    run_id: str,
    script_id: str,
    dim_scores: list[DimensionScore],
    signal_values: dict[str, SignalValue],
    template_dir: str | None = None,
) -> list[ImprovementAction]:
    templates = _load_templates(Path(template_dir) if template_dir else _DEFAULT_TEMPLATE_DIR)
    if not templates:
        return []

    out: list[ImprovementAction] = []
    emitted: set[tuple[str, str]] = set()
    for dim in dim_scores:
        if dim.tier not in _ACTIONABLE_TIERS:
            continue
        for signal_ref in dim.signal_refs:
            signal_key = str(signal_ref.get("signal_key") or "").strip()
            if not signal_key:
                continue
            template = templates.get(signal_key)
            if template is None:
                continue
            dedup_key = (signal_key, dim.tier)
            if dedup_key in emitted:
                continue

            tiers = template.get("tiers") or {}
            if not isinstance(tiers, dict):
                continue
            tier_template = tiers.get(dim.tier)
            if not isinstance(tier_template, dict):
                continue

            signal = signal_values.get(signal_key)
            evidence_refs = signal.evidence_refs if signal is not None else []
            if not evidence_refs:
                continue
            evidence_refs = [ref for ref in evidence_refs if isinstance(ref, dict)]
            if not evidence_refs:
                continue

            context: dict[str, Any] = {
                "dimension": dim.dimension,
                "tier": dim.tier,
                "score": signal.score if signal is not None else signal_ref.get("score"),
                "coverage_ratio": dim.coverage_ratio,
                "signal_key": signal_key,
            }
            if signal is not None and isinstance(signal.value, dict):
                context.update(signal.value)
            elif isinstance(signal_ref.get("value"), dict):
                context.update(signal_ref["value"])

            issue = _render_template(str(tier_template.get("issue_template") or ""), context)
            target = _render_template(str(tier_template.get("target_template") or ""), context)
            step_templates = tier_template.get("action_steps_template") or []
            if not isinstance(step_templates, list):
                step_templates = []
            action_steps = [
                _render_template(str(item), context)
                for item in step_templates
                if str(item).strip()
            ]
            estimated_lift_rule = tier_template.get("estimated_lift_rule") or {}
            estimated_lift: dict[str, float] = {}
            if isinstance(estimated_lift_rule, dict):
                for key, value in estimated_lift_rule.items():
                    try:
                        estimated_lift[str(key)] = float(value)
                    except (TypeError, ValueError):
                        continue
            if not issue or not target or not action_steps:
                continue

            out.append(
                ImprovementAction(
                    id=str(uuid.uuid4()),
                    run_id=run_id,
                    script_id=script_id,
                    dimension=dim.dimension,
                    signal_key=signal_key,
                    template_id=f"{signal_key}:{template.get('template_version') or 'v1'}",
                    issue=issue,
                    target=target,
                    action_steps=action_steps,
                    evidence_refs=evidence_refs,
                    estimated_lift=estimated_lift,
                )
            )
            emitted.add(dedup_key)
    return out
