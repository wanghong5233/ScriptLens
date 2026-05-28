from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from service.script_report_service import ingest_dataset as ingest_dataset_service

_DATASET_RELATIVE = Path("eval") / "ai漫剧剧本数据集" / "完整本"


def _resolve_scriptlens_root() -> Path:
    """与 cli/run_stability.py 同源逻辑：marker 扫描 + env override，
    避免容器内 Path(__file__).parents[3] IndexError。"""
    env_root = os.getenv("SCRIPTLENS_ROOT")
    if env_root:
        return Path(env_root)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / _DATASET_RELATIVE).is_dir():
            return parent
    if (Path("/") / _DATASET_RELATIVE).is_dir():
        return Path("/")
    parents = list(here.parents)
    return parents[-1] if parents else here


_SCRIPTLENS_ROOT = _resolve_scriptlens_root()
_DEFAULT_DATASET_DIR = _SCRIPTLENS_ROOT / _DATASET_RELATIVE
_DEFAULT_SUMMARY_PATH = Path(__file__).resolve().parents[1] / "eval" / "reports" / "dataset_ingest_summary.json"


def ingest_dataset(
    *,
    dataset_dir: Path,
    user_id: int,
    skip_unsupported: bool,
    limit: int | None,
    summary_output: Path = _DEFAULT_SUMMARY_PATH,
) -> dict[str, Any]:
    return ingest_dataset_service(
        dataset_dir=dataset_dir,
        user_id=user_id,
        skip_unsupported=skip_unsupported,
        limit=limit,
        summary_output=summary_output,
    )


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
