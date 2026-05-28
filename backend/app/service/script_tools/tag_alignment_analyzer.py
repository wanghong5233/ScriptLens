from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from service.script_tools.match_config import SharedGate, SharedTagEntry


DEFAULT_TARGET_DIMS: tuple[dict[str, str], ...] = (
    {"dim": "world_setting", "scope": "script", "tag_set_ver": "script"},
    {"dim": "scene_emotion_keynote", "scope": "plot_unit", "tag_set_ver": "script"},
    {"dim": "relationship_polarity", "scope": "relationship", "tag_set_ver": "script"},
    {"dim": "gender_axis", "scope": "script", "tag_set_ver": "script"},
    {"dim": "scene_locale_type", "scope": "plot_unit", "tag_set_ver": "script"},
)


@dataclass(frozen=True)
class VideoSideEntry:
    dim: str
    verdict: str
    par: float | None = None
    stable: int | None = None
    stable_total: int | None = None
    wilson_low: float | None = None


def _coerce_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _coerce_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def load_script_side(per_dim_dirs: list[Path], dims: list[str]) -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = {}
    for root in per_dim_dirs:
        if not root.exists():
            continue
        for fp in sorted(root.glob("*.json")):
            payload = json.loads(fp.read_text(encoding="utf-8"))
            dim = str(payload.get("dim") or fp.stem)
            if dim not in dims:
                continue
            data[dim] = payload
    missing = [dim for dim in dims if dim not in data]
    if missing:
        raise ValueError(f"missing script-side stability json for dims: {missing}")
    return data


def load_video_side(snapshot_yaml: Path) -> dict[str, VideoSideEntry]:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError("pyyaml is required for tag_alignment_analyzer") from exc

    payload = yaml.safe_load(snapshot_yaml.read_text(encoding="utf-8")) or {}
    dims = payload.get("dims") or {}
    if not isinstance(dims, dict):
        raise ValueError(f"invalid video snapshot dims in {snapshot_yaml}")
    out: dict[str, VideoSideEntry] = {}
    for dim, raw in dims.items():
        row = raw if isinstance(raw, dict) else {}
        out[str(dim)] = VideoSideEntry(
            dim=str(dim),
            verdict=str(row.get("verdict") or "video_blocked"),
            par=_coerce_float(row.get("par")),
            stable=_coerce_int(row.get("stable")),
            stable_total=_coerce_int(row.get("stable_total")),
            wilson_low=_coerce_float(row.get("wilson_low")),
        )
    return out


def decide_gate(script_report: dict[str, Any], video_entry: VideoSideEntry) -> tuple[SharedGate, str]:
    script_verdict = str(script_report.get("verdict") or "")
    video_verdict = video_entry.verdict

    if script_verdict == "offline" or video_verdict == "video_blocked":
        return SharedGate.BLOCKED, "at least one side is blocked/offline"
    if script_verdict == "fix" or video_verdict == "video_experimental":
        return SharedGate.EXPERIMENTAL, "at least one side is experimental/fix"
    if script_verdict == "online" and video_verdict == "video_stable":
        return SharedGate.STABLE_SHARED, "both sides are stable"
    if script_verdict == "online" and video_verdict == "video_aggregate":
        return SharedGate.AGGREGATE_ONLY, "video side requires aggregation"
    return SharedGate.BLOCKED, f"unsupported verdict combination: {script_verdict}/{video_verdict}"


def build_alignment_entries(
    *,
    target_dims: tuple[dict[str, str], ...],
    script_reports: dict[str, dict[str, Any]],
    video_entries: dict[str, VideoSideEntry],
) -> list[SharedTagEntry]:
    entries: list[SharedTagEntry] = []
    for meta in target_dims:
        dim = meta["dim"]
        if dim not in script_reports:
            raise ValueError(f"script report missing for dim={dim}")
        if dim not in video_entries:
            raise ValueError(f"video snapshot missing for dim={dim}")
        script_report = script_reports[dim]
        video_entry = video_entries[dim]
        gate, reason = decide_gate(script_report, video_entry)
        entries.append(
            SharedTagEntry(
                dim=dim,
                scope=meta["scope"],
                gate=gate,
                script_verdict=str(script_report.get("verdict") or ""),
                video_verdict=video_entry.verdict,
                reason=reason,
                tag_set_ver=meta["tag_set_ver"],
            )
        )
    return entries


def make_gates_module(entries: list[SharedTagEntry]) -> str:
    lines = [
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "from enum import Enum",
        "",
        "",
        "class SharedGate(str, Enum):",
        '    STABLE_SHARED = "stable_shared"',
        '    AGGREGATE_ONLY = "aggregate_only"',
        '    EXPERIMENTAL = "experimental"',
        '    BLOCKED = "blocked"',
        "",
        "",
        "@dataclass(frozen=True)",
        "class SharedTagEntry:",
        "    dim: str",
        "    scope: str",
        "    gate: SharedGate",
        "    script_verdict: str",
        "    video_verdict: str",
        "    reason: str",
        "    tag_set_ver: str",
        "",
        "",
        "# AUTOGENERATED BELOW. Regenerated by `python -m cli.run_cross_modal_alignment`.",
        "SHARED_TAG_GATES: tuple[SharedTagEntry, ...] = (",
    ]
    for entry in entries:
        lines.extend(
            [
                "    SharedTagEntry(",
                f'        dim="{entry.dim}",',
                f'        scope="{entry.scope}",',
                f"        gate=SharedGate.{entry.gate.name},",
                f'        script_verdict="{entry.script_verdict}",',
                f'        video_verdict="{entry.video_verdict}",',
                f'        reason="{entry.reason}",',
                f'        tag_set_ver="{entry.tag_set_ver}",',
                "    ),",
            ]
        )
    lines.extend(
        [
            ")",
            "",
            "",
            "def list_entries() -> tuple[SharedTagEntry, ...]:",
            "    return SHARED_TAG_GATES",
            "",
            "",
            "def list_dims_by_gate(gate: SharedGate | str) -> tuple[str, ...]:",
            "    gate_value = SharedGate(gate)",
            "    return tuple(entry.dim for entry in SHARED_TAG_GATES if entry.gate == gate_value)",
            "",
            "",
            "def get_entry(dim: str) -> SharedTagEntry | None:",
            "    for entry in SHARED_TAG_GATES:",
            "        if entry.dim == dim:",
            "            return entry",
            "    return None",
            "",
        ]
    )
    return "\n".join(lines)


def render_match_config(entries: list[SharedTagEntry], out_py: Path) -> None:
    out_py.parent.mkdir(parents=True, exist_ok=True)
    out_py.write_text(make_gates_module(entries), encoding="utf-8")


def _render_gate_list(entries: list[SharedTagEntry], gate: SharedGate) -> list[str]:
    dims = [entry.dim for entry in entries if entry.gate == gate]
    if not dims:
        return [f"- `{gate.value}`: (empty)"]
    return [f"- `{gate.value}`: {', '.join(dims)}"]


def render_markdown_report(
    *,
    entries: list[SharedTagEntry],
    script_reports: dict[str, dict[str, Any]],
    video_entries: dict[str, VideoSideEntry],
    out_md: Path,
) -> None:
    lines = [
        "# Cross-Modal Tag Alignment",
        "",
        "## Gate Summary",
        "",
        "| dim | scope | tag_set_ver | script_verdict | video_verdict | gate | reason |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        lines.append(
            f"| {entry.dim} | {entry.scope} | {entry.tag_set_ver} | {entry.script_verdict} | "
            f"{entry.video_verdict} | {entry.gate.value} | {entry.reason} |"
        )

    lines.extend(
        [
            "",
            "## Shared Gate Lists",
            "",
            *_render_gate_list(entries, SharedGate.STABLE_SHARED),
            *_render_gate_list(entries, SharedGate.AGGREGATE_ONLY),
            *_render_gate_list(entries, SharedGate.EXPERIMENTAL),
            *_render_gate_list(entries, SharedGate.BLOCKED),
            "",
            "## Script-Side Metrics",
            "",
            "| dim | intra_alpha | inter_alpha | kappa_mean | par | unstable_values |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for entry in entries:
        row = script_reports.get(entry.dim) or {}
        unstable_values = ", ".join(str(v) for v in (row.get("unstable_values") or []))
        lines.append(
            f"| {entry.dim} | {float(row.get('intra_alpha', 0.0)):.3f} | "
            f"{float(row.get('inter_alpha', 0.0)):.3f} | {float(row.get('kappa_mean', 0.0)):.3f} | "
            f"{float(row.get('par', 0.0)):.3f} | {unstable_values or '-'} |"
        )

    lines.extend(
        [
            "",
            "## Video-Side Snapshot",
            "",
            "| dim | verdict | par | stable | stable_total | wilson_low |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for entry in entries:
        video = video_entries[entry.dim]
        lines.append(
            f"| {entry.dim} | {video.verdict} | {video.par or 0.0:.3f} | {video.stable or 0} | "
            f"{video.stable_total or 0} | {video.wilson_low or 0.0:.3f} |"
        )

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines), encoding="utf-8")


def render_json_deliverable(
    *,
    entries: list[SharedTagEntry],
    script_reports: dict[str, dict[str, Any]],
    video_entries: dict[str, VideoSideEntry],
    out_json: Path,
) -> None:
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entries": [],
    }
    rows: list[dict[str, Any]] = []
    for entry in entries:
        script = script_reports[entry.dim]
        video = video_entries[entry.dim]
        rows.append(
            {
                "dim": entry.dim,
                "scope": entry.scope,
                "tag_set_ver": entry.tag_set_ver,
                "gate": entry.gate.value,
                "script_verdict": entry.script_verdict,
                "video_verdict": entry.video_verdict,
                "reason": entry.reason,
                "script_metrics": {
                    "intra_alpha": _coerce_float(script.get("intra_alpha")),
                    "inter_alpha": _coerce_float(script.get("inter_alpha")),
                    "kappa_mean": _coerce_float(script.get("kappa_mean")),
                    "par": _coerce_float(script.get("par")),
                    "unstable_values": list(script.get("unstable_values") or []),
                },
                "video_metrics": {
                    "par": video.par,
                    "stable": video.stable,
                    "stable_total": video.stable_total,
                    "wilson_low": video.wilson_low,
                },
            }
        )
    payload["entries"] = rows
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
