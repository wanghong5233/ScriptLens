from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from service.core.ingestion.script_loader import UnsupportedScriptFormatError
from service.script_ingestion_service import ScriptIngestionService

_SCRIPTLENS_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DATASET_DIR = _SCRIPTLENS_ROOT / "eval" / "ai漫剧剧本数据集" / "完整本"
_DEFAULT_SUMMARY_PATH = Path(__file__).resolve().parents[1] / "eval" / "reports" / "dataset_ingest_summary.json"


def _list_dataset_files(dataset_dir: Path) -> list[Path]:
    if not dataset_dir.exists():
        return []
    files = [path for path in dataset_dir.iterdir() if path.is_file()]
    files.sort(key=lambda path: path.name)
    return files


def _is_skippable_error(exc: Exception) -> bool:
    if isinstance(exc, UnsupportedScriptFormatError):
        return True
    if isinstance(exc, ValueError):
        msg = str(exc)
        if "段落为空" in msg or "无场景" in msg:
            return True
    return False


def ingest_dataset(
    *,
    dataset_dir: Path,
    user_id: int,
    skip_unsupported: bool,
    limit: int | None,
    summary_output: Path = _DEFAULT_SUMMARY_PATH,
) -> dict[str, Any]:
    files = _list_dataset_files(dataset_dir)
    if limit is not None and limit > 0:
        files = files[:limit]

    ingest_service = ScriptIngestionService()
    ok_rows: list[dict[str, Any]] = []
    failed_rows: list[dict[str, str]] = []
    mapping: dict[str, str] = {}

    for file_path in files:
        try:
            result = ingest_service.ingest(
                file_path=file_path,
                user_id=user_id,
                title=file_path.stem,
            )
            mapping[file_path.name] = result.script_id
            ok_rows.append(
                {
                    "path": str(file_path),
                    "script_id": result.script_id,
                    "title": result.title,
                    "total_episodes": result.total_episodes,
                    "total_scenes": result.total_scenes,
                }
            )
        except Exception as exc:  # pragma: no cover - runtime integration branch
            if skip_unsupported and _is_skippable_error(exc):
                failed_rows.append({"path": str(file_path), "reason": f"{type(exc).__name__}: {exc}"})
                continue
            failed_rows.append({"path": str(file_path), "reason": f"{type(exc).__name__}: {exc}"})

    payload = {
        "dataset_dir": str(dataset_dir),
        "total": len(files),
        "ok": ok_rows,
        "failed": failed_rows,
        "mapping": mapping,
    }
    summary_output.parent.mkdir(parents=True, exist_ok=True)
    summary_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bulk ingest script dataset into scriptlens.scripts/scenes.")
    parser.add_argument(
        "--dataset-dir",
        default=str(_DEFAULT_DATASET_DIR),
        help="Dataset directory that contains script files.",
    )
    parser.add_argument("--user-id", type=int, default=1, help="owner user id for inserted scripts.")
    parser.add_argument(
        "--skip-unsupported",
        action="store_true",
        help="Skip unsupported files (e.g. .doc) and zero-content parses.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Process at most N files. 0 means no limit.")
    parser.add_argument(
        "--summary-output",
        default=str(_DEFAULT_SUMMARY_PATH),
        help="Path of dataset ingest summary JSON.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = ingest_dataset(
        dataset_dir=Path(args.dataset_dir),
        user_id=int(args.user_id),
        skip_unsupported=bool(args.skip_unsupported),
        limit=None if int(args.limit) <= 0 else int(args.limit),
        summary_output=Path(args.summary_output),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
