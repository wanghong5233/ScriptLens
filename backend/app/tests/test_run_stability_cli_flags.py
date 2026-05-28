from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli import run_stability as cli


def test_parse_args_rejects_legacy_flags(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["run_stability.py", "--tag-set", "script", "--intra", "5"],
    )
    with pytest.raises(SystemExit):
        cli._parse_args()


def test_parse_args_accepts_concurrency_and_sample_size(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_stability.py",
            "--tag-set",
            "script",
            "--sample-size",
            "30",
            "--concurrency",
            "8",
        ],
    )
    args = cli._parse_args()
    assert args.sample_size == 30
    assert args.concurrency == 8
    assert args.concurrency_hi == 64
    assert args.include_segmentation is True
    assert args.disable_cache is True


def test_run_tag_value_stability_uses_bundle_loop(monkeypatch) -> None:
    class _FakeExpDir:
        def __init__(self) -> None:
            self._samples: dict[str, list[str]] = {}
            self.progress: list[dict[str, object]] = []

        def load_samples(self) -> dict[str, list[str]]:
            return dict(self._samples)

        def save_samples(self, samples: dict[str, list[str]]) -> None:
            self._samples = {k: list(v) for k, v in samples.items()}

        async def update_progress(
            self,
            layer: str,
            *,
            completed: int,
            total: int,
            scope: str | None = None,
        ) -> None:
            self.progress.append(
                {"layer": layer, "scope": scope, "completed": completed, "total": total}
            )

    captured: dict[str, object] = {"calls": []}

    def _fake_bundles(tag_set_ver: str, *, scope: str | None = None):  # noqa: ARG001
        all_bundles = [
            SimpleNamespace(id="b_script_main", scope="script", dims=("d1", "d2")),
            SimpleNamespace(id="b_script_ref", scope="script", dims=("d3",)),
            SimpleNamespace(id="b_plot", scope="plot_unit", dims=("d4",)),
        ]
        if scope is None:
            return all_bundles
        return [bundle for bundle in all_bundles if bundle.scope == scope]

    async def _fake_bundle_runner(task, **kwargs):  # noqa: ANN001, ANN003
        captured["calls"].append(
            {
                "bundle_id": task.bundle_id,
                "scope": task.scope,
                "dims": list(task.dims),
                "targets": list(task.targets),
                "n_repeats": kwargs["n_repeats"],
            }
        )
        return {dim: SimpleNamespace(task=None, intra_runs=[], inter_runs=[]) for dim in task.dims}

    monkeypatch.setattr(cli, "_all_bundles", _fake_bundles)
    monkeypatch.setattr(cli, "_sample_targets", lambda *args, **kwargs: ["sid-1", "sid-2"])
    monkeypatch.setattr(cli, "_run_tag_value_for_bundle_concurrent", _fake_bundle_runner)

    exp_dir = _FakeExpDir()
    result = cli.asyncio.run(
        cli._run_tag_value_stability(
            scopes=["script"],
            script_ids=["sid-1", "sid-2", "sid-3"],
            tag_set_ver="script",
            sample_size=50,
            seed=42,
            n_repeats=5,
            dims_filter={"d1", "d2", "d3"},
            exp_dir=exp_dir,  # type: ignore[arg-type]
            aimd=SimpleNamespace(),
            use_cache=False,
        )
    )

    calls = captured["calls"]
    assert isinstance(calls, list)
    assert len(calls) == 2
    assert {item["bundle_id"] for item in calls} == {"b_script_main", "b_script_ref"}
    assert set(result.keys()) == {"d1", "d2", "d3"}
    # progress total = targets(2) * repeats(5) * bundles(2)
    assert any(p["scope"] == "script" and p["total"] == 20 for p in exp_dir.progress)
