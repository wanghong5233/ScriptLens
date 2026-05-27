from pathlib import Path

from service.script_tools.dimension_aggregator import DimensionScore
from service.script_tools.improvement_action_generator import generate_actions
from service.script_tools.signal_catalog import SignalValue


def _write_template(path: Path) -> None:
    path.write_text(
        """
signal_key: opening_speed
template_version: v3.0.0
tiers:
  poor:
    issue_template: "首钩子过晚：{opening_index}/{total_units}"
    target_template: "前 5% 内出现主冲突"
    action_steps_template:
      - "把首钩子前移到第 1 个 plot_unit"
      - "补主角目标和代价"
    estimated_lift_rule:
      pacing: 1.5
  weak:
    issue_template: "开场略慢"
    target_template: "前 10% 内触发冲突"
    action_steps_template:
      - "压缩铺垫"
    estimated_lift_rule:
      pacing: 0.8
""".strip(),
        encoding="utf-8",
    )


def test_generate_actions_from_templates(tmp_path: Path) -> None:
    template_file = tmp_path / "opening_speed.yaml"
    _write_template(template_file)

    dim_scores = [
        DimensionScore(
            dimension="pacing",
            score=3.2,
            coverage_ratio=1.0,
            confidence="medium",
            tier="poor",
            signal_refs=[
                {
                    "signal_key": "opening_speed",
                    "score": 3.2,
                    "value": {"opening_index": 6, "total_units": 30},
                    "source": "rule",
                    "confidence": 0.8,
                    "weight_in_dim": 0.2,
                    "primary_dimension": "pacing",
                    "evidence_refs": [{"scene_id": "scene-1"}],
                }
            ],
            primary_dimension="pacing",
            reason="",
        ),
    ]
    signal_values = {
        "opening_speed": SignalValue(
            key="opening_speed",
            value={"opening_index": 6, "total_units": 30},
            score=3.2,
            source="rule",
            confidence=0.8,
            evidence_refs=[{"scene_id": "scene-1"}],
        )
    }
    actions = generate_actions(
        run_id="run-1",
        script_id="script-1",
        dim_scores=dim_scores,
        signal_values=signal_values,
        template_dir=str(tmp_path),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action.signal_key == "opening_speed"
    assert "6/30" in action.issue
    assert action.action_steps
    assert action.evidence_refs
    assert action.estimated_lift.get("pacing") == 1.5


def test_generate_actions_skips_missing_evidence(tmp_path: Path) -> None:
    template_file = tmp_path / "opening_speed.yaml"
    _write_template(template_file)
    dim_scores = [
        DimensionScore(
            dimension="pacing",
            score=3.0,
            coverage_ratio=1.0,
            confidence="low",
            tier="poor",
            signal_refs=[
                {
                    "signal_key": "opening_speed",
                    "score": 3.0,
                    "value": {"opening_index": 8, "total_units": 30},
                    "source": "rule",
                    "confidence": 0.5,
                    "weight_in_dim": 0.2,
                    "primary_dimension": "pacing",
                    "evidence_refs": [],
                }
            ],
            primary_dimension="pacing",
            reason="",
        ),
    ]
    signal_values = {
        "opening_speed": SignalValue(
            key="opening_speed",
            value={"opening_index": 8, "total_units": 30},
            score=3.0,
            source="rule",
            confidence=0.5,
            evidence_refs=[],
        )
    }
    actions = generate_actions(
        run_id="run-1",
        script_id="script-1",
        dim_scores=dim_scores,
        signal_values=signal_values,
        template_dir=str(tmp_path),
    )
    assert actions == []
