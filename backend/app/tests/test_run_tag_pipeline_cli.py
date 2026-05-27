import json

import pytest

from cli import run_tag_pipeline as cli
from service.script_tools.tag_pipeline import PipelineRunSummary


def test_parse_args_requires_tag_set(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["run_tag_pipeline.py"])
    with pytest.raises(SystemExit):
        cli._parse_args()


def test_main_outputs_summary_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_tag_pipeline.py",
            "--tag-set",
            "v2.0.0",
            "--seed",
            "11",
            "--variant",
            "c",
        ],
    )
    monkeypatch.setattr(cli, "_latest_script_id", lambda: "sid-latest")

    async def _fake_run_tag_pipeline(script_ref: str, **kwargs):  # noqa: ANN003
        assert script_ref == "sid-latest"
        assert kwargs["tag_set_ver"] == "v2.0.0"
        assert kwargs["seed"] == 11
        assert kwargs["variant"] == "c"
        return PipelineRunSummary(
            script_id="sid-latest",
            tag_set_ver="v2.0.0",
            seed=11,
            variant="c",
            plot_unit_count=6,
            character_entity_count=4,
            relationship_count=3,
            bundle_runs={"v2_storyboard_hints": 6},
        )

    monkeypatch.setattr(cli, "run_tag_pipeline", _fake_run_tag_pipeline)

    cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["script_id"] == "sid-latest"
    assert payload["tag_set_ver"] == "v2.0.0"
    assert payload["seed"] == 11
    assert payload["variant"] == "c"
    assert payload["plot_unit_count"] == 6
    assert payload["character_entity_count"] == 4
    assert payload["relationship_count"] == 3
    assert payload["bundle_runs"] == {"v2_storyboard_hints": 6}
