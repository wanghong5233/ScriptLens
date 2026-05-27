from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import run_stability as cli
from eval.stability.layer_a_runner import LayerAReport


def test_parse_args_rejects_legacy_flags(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["run_stability.py", "--tag-set", "v2.0.0", "--intra", "5"],
    )
    with pytest.raises(SystemExit):
        cli._parse_args()


def test_run_stability_passes_new_flags(monkeypatch, tmp_path) -> None:
    captured: dict = {}

    class _FakeExpDir:
        def __init__(self, run_dir: Path, scripts: list[str]) -> None:
            self.run_dir = run_dir
            self.run_id = "rid"
            self.manifest = {"stages": {}, "scripts": scripts}

        @staticmethod
        def default_root() -> Path:
            return tmp_path

        @staticmethod
        def create(**kwargs):  # noqa: ANN003
            run_dir = tmp_path / kwargs["run_id"]
            run_dir.mkdir(parents=True, exist_ok=True)
            captured["create_kwargs"] = kwargs
            return _FakeExpDir(run_dir=run_dir, scripts=list(kwargs["scripts"]))

        @staticmethod
        def load(run_id: str):  # noqa: ARG004
            raise AssertionError("resume path should not run in this test")

        def update_stage(self, stage: str, status: str) -> None:
            self.manifest.setdefault("stages", {})[stage] = status

        def save_aggregated_layer_a(self, reports):  # noqa: ANN001, ANN201
            captured["layer_a_reports"] = reports

        def save_aggregated_layer_b(self, reports):  # noqa: ANN001, ANN201
            captured["layer_b_reports"] = reports

    async def _fake_bootstrap(**kwargs):  # noqa: ANN003
        captured["bootstrap"] = kwargs

    async def _fake_layer_a_repeat(*args, **kwargs):  # noqa: ANN002, ANN003
        return LayerAReport(
            script_id="sid-1",
            n_scenes=5,
            unit_count_mean=3.0,
            unit_count_cv=0.0,
            window_diff_mean=0.0,
            boundary_similarity_mean=1.0,
            verdict="stable",
        )

    async def _fake_run_intra_pss(task, *, seed, n_repeats, exp_dir, use_cache):  # noqa: ANN001
        captured["run_intra"] = {
            "seed": seed,
            "n_repeats": n_repeats,
            "use_cache": use_cache,
            "targets": list(task.targets),
            "scope": task.scope,
            "dim": task.dim,
            "exp_dir": exp_dir,
        }
        return SimpleNamespace(intra_runs=[], inter_runs=[], task=task, matrix=lambda run_type: [])

    monkeypatch.setattr(cli, "ExperimentDir", _FakeExpDir)
    monkeypatch.setattr(cli, "register_bundle_scope_extractors", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "ingest_dataset",
        lambda **kwargs: {"ok": [{"script_id": "sid-1"}], "failed": [], "mapping": {"A.txt": "sid-1"}},
    )
    monkeypatch.setattr(cli, "_bootstrap_structures", _fake_bootstrap)
    monkeypatch.setattr(cli, "run_layer_a_repeat", _fake_layer_a_repeat)
    monkeypatch.setattr(cli, "_build_targets", lambda scope, script_ids, tag_set_ver: ["sid-1"])
    monkeypatch.setattr(cli, "run_intra_pss", _fake_run_intra_pss)
    monkeypatch.setattr(cli, "aggregate", lambda run_results: {"dim_a": {"dim": "dim_a", "par": 1.0, "n_samples": 1, "stable_count": 1, "wilson_lower": 1.0}})
    monkeypatch.setattr(cli, "write_markdown", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "generate_script_tag_stability_decision",
        lambda run_dir: {"run_decision_md": str(Path(run_dir) / "decision.md"), "project_decision_md": str(tmp_path / "docs.md")},
    )
    monkeypatch.setattr(cli.ExtractorRegistry, "has", classmethod(lambda cls, *_args, **_kwargs: True))
    monkeypatch.setattr(cli.ExtractorRegistry, "register", classmethod(lambda cls, *_args, **_kwargs: None))
    monkeypatch.setattr(
        cli,
        "load_tag_set",
        lambda *_args, **_kwargs: SimpleNamespace(
            scope_to_dims={"script": [SimpleNamespace(dim="dim_a")]},
        ),
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_stability.py",
            "--tag-set",
            "v2.0.0",
            "--scope",
            "script",
            "--dims",
            "dim_a",
            "--n-scripts",
            "5",
            "--n-repeats",
            "7",
            "--seed",
            "99",
            "--run-id",
            "run_demo",
            "--disable-cache",
            "--include-layer-a",
            "--source-dir",
            str(tmp_path / "dataset"),
        ],
    )

    cli.main()
    run_intra = captured["run_intra"]
    assert run_intra["seed"] == 99
    assert run_intra["n_repeats"] == 7
    assert run_intra["use_cache"] is False
    assert run_intra["targets"] == ["sid-1"]
    assert run_intra["scope"] == "script"
    assert run_intra["dim"] == "dim_a"
