from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cli import ingest_dataset as cli
from service.core.ingestion.script_loader import UnsupportedScriptFormatError


@dataclass
class _FakeIngestResult:
    script_id: str
    title: str
    total_episodes: int
    total_scenes: int


def test_ingest_dataset_skip_unsupported_and_write_summary(tmp_path, monkeypatch) -> None:
    dataset_files = [
        tmp_path / "A.doc",
        tmp_path / "B.pdf",
        tmp_path / "C.txt",
    ]

    class _FakeService:
        def ingest(self, *, file_path: Path, user_id: int, title: str):  # noqa: ANN003
            assert user_id == 7
            if file_path.suffix == ".doc":
                raise UnsupportedScriptFormatError("doc unsupported")
            if file_path.suffix == ".pdf":
                raise ValueError("剧本解析后段落为空：B.pdf")
            return _FakeIngestResult(
                script_id="sid-c",
                title=title,
                total_episodes=10,
                total_scenes=120,
            )

    monkeypatch.setattr(cli, "_list_dataset_files", lambda _dataset_dir: dataset_files)
    monkeypatch.setattr(cli, "ScriptIngestionService", _FakeService)
    summary_path = tmp_path / "summary.json"

    payload = cli.ingest_dataset(
        dataset_dir=tmp_path,
        user_id=7,
        skip_unsupported=True,
        limit=None,
        summary_output=summary_path,
    )

    assert payload["total"] == 3
    assert len(payload["ok"]) == 1
    assert len(payload["failed"]) == 2
    assert payload["mapping"] == {"C.txt": "sid-c"}
    assert summary_path.exists()
    written = json.loads(summary_path.read_text(encoding="utf-8"))
    assert written["mapping"]["C.txt"] == "sid-c"
    failed_paths = {item["path"] for item in written["failed"]}
    assert str(tmp_path / "A.doc") in failed_paths
    assert str(tmp_path / "B.pdf") in failed_paths
