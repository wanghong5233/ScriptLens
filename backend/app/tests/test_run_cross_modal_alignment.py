import json
from pathlib import Path

from cli.run_cross_modal_alignment import main


def _write_script_report(root: Path, dim: str, verdict: str = "online") -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "dim": dim,
        "intra_alpha": 0.91,
        "inter_alpha": 0.84,
        "kappa_mean": 0.8,
        "par": 0.82,
        "cosine": 0.9,
        "macro_f1": None,
        "verdict": verdict,
        "unstable_values": [],
        "genre_sensitive": [],
        "confusion": None,
    }
    (root / f"{dim}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_video_snapshot(path: Path) -> None:
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("pyyaml is required for test_run_cross_modal_alignment") from exc

    payload = {
        "source": "unit-test",
        "model": "mock",
        "n_samples": 50,
        "n_repeats": 5,
        "dims": {
            "world_setting": {"verdict": "video_stable", "par": 1.0, "stable": 50, "stable_total": 50, "wilson_low": 0.9},
            "scene_emotion_keynote": {
                "verdict": "video_stable",
                "par": 0.99,
                "stable": 48,
                "stable_total": 50,
                "wilson_low": 0.86,
            },
            "relationship_polarity": {
                "verdict": "video_experimental",
                "par": 0.95,
                "stable": 43,
                "stable_total": 50,
                "wilson_low": 0.73,
            },
            "gender_axis": {"verdict": "video_aggregate", "par": 0.95, "stable": 44, "stable_total": 50, "wilson_low": 0.76},
            "scene_locale_type": {
                "verdict": "video_aggregate",
                "par": 0.96,
                "stable": 44,
                "stable_total": 50,
                "wilson_low": 0.76,
            },
        },
    }
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_main_dry_run(monkeypatch, capsys, tmp_path: Path) -> None:
    d1 = tmp_path / "v1"
    d2 = tmp_path / "v2"
    for dim in ("world_setting", "gender_axis", "relationship_polarity"):
        _write_script_report(d1, dim)
    for dim in ("scene_emotion_keynote", "scene_locale_type"):
        _write_script_report(d2, dim)
    snapshot = tmp_path / "video.yaml"
    _write_video_snapshot(snapshot)

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_cross_modal_alignment.py",
            "--script-stability-dir",
            str(d1),
            "--script-stability-dir",
            str(d2),
            "--video-snapshot",
            str(snapshot),
            "--dry-run",
        ],
    )
    main()
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["gate_count"]["stable_shared"] == 2
    assert len(payload["entries"]) == 5


def test_main_writes_outputs(monkeypatch, capsys, tmp_path: Path) -> None:
    d1 = tmp_path / "v1"
    d2 = tmp_path / "v2"
    for dim in ("world_setting", "gender_axis", "relationship_polarity"):
        _write_script_report(d1, dim)
    for dim in ("scene_emotion_keynote", "scene_locale_type"):
        _write_script_report(d2, dim)
    snapshot = tmp_path / "video.yaml"
    _write_video_snapshot(snapshot)

    out_config = tmp_path / "match_config.py"
    out_md = tmp_path / "alignment.md"
    out_json = tmp_path / "alignment.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_cross_modal_alignment.py",
            "--script-stability-dir",
            str(d1),
            "--script-stability-dir",
            str(d2),
            "--video-snapshot",
            str(snapshot),
            "--out-config",
            str(out_config),
            "--out-md",
            str(out_md),
            "--out-json",
            str(out_json),
        ],
    )
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is False
    assert out_config.exists()
    assert out_md.exists()
    assert out_json.exists()
    assert "relationship_polarity" in out_config.read_text(encoding="utf-8")
    assert "Cross-Modal Tag Alignment" in out_md.read_text(encoding="utf-8")
    entries = json.loads(out_json.read_text(encoding="utf-8"))["entries"]
    assert len(entries) == 5

