import importlib.util
import sys
from pathlib import Path

from service.script_tools.match_config import SharedGate, SharedTagEntry
from service.script_tools.tag_alignment_analyzer import VideoSideEntry, decide_gate, render_match_config


def test_decide_gate_matrix_16_combinations() -> None:
    expected = {
        ("online", "video_stable"): SharedGate.STABLE_SHARED,
        ("online", "video_aggregate"): SharedGate.AGGREGATE_ONLY,
        ("online", "video_experimental"): SharedGate.EXPERIMENTAL,
        ("online", "video_blocked"): SharedGate.BLOCKED,
        ("fix", "video_stable"): SharedGate.EXPERIMENTAL,
        ("fix", "video_aggregate"): SharedGate.EXPERIMENTAL,
        ("fix", "video_experimental"): SharedGate.EXPERIMENTAL,
        ("fix", "video_blocked"): SharedGate.BLOCKED,
        ("offline", "video_stable"): SharedGate.BLOCKED,
        ("offline", "video_aggregate"): SharedGate.BLOCKED,
        ("offline", "video_experimental"): SharedGate.BLOCKED,
        ("offline", "video_blocked"): SharedGate.BLOCKED,
        ("unknown", "video_stable"): SharedGate.BLOCKED,
        ("unknown", "video_aggregate"): SharedGate.BLOCKED,
        ("unknown", "video_experimental"): SharedGate.EXPERIMENTAL,
        ("unknown", "video_blocked"): SharedGate.BLOCKED,
    }

    for script_verdict in ("online", "fix", "offline", "unknown"):
        for video_verdict in ("video_stable", "video_aggregate", "video_experimental", "video_blocked"):
            gate, _reason = decide_gate(
                {"verdict": script_verdict},
                VideoSideEntry(dim="world_setting", verdict=video_verdict),
            )
            assert gate == expected[(script_verdict, video_verdict)]


def test_render_match_config_module_is_importable(tmp_path: Path) -> None:
    entries = [
        SharedTagEntry(
            dim="world_setting",
            scope="script",
            gate=SharedGate.STABLE_SHARED,
            script_verdict="online",
            video_verdict="video_stable",
            reason="both sides are stable",
            tag_set_ver="v1.0.0",
        ),
        SharedTagEntry(
            dim="scene_locale_type",
            scope="plot_unit",
            gate=SharedGate.AGGREGATE_ONLY,
            script_verdict="online",
            video_verdict="video_aggregate",
            reason="video side requires aggregation",
            tag_set_ver="v2.0.0",
        ),
    ]
    output = tmp_path / "match_config_generated.py"
    render_match_config(entries, output)

    spec = importlib.util.spec_from_file_location("generated_match_config", output)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert len(module.SHARED_TAG_GATES) == 2
    assert module.get_entry("world_setting") is not None
    assert module.list_dims_by_gate(module.SharedGate.STABLE_SHARED) == ("world_setting",)

