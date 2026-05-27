from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

StageStatus = Literal["pending", "in_progress", "done"]

_REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "script_stability_v2"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if is_dataclass(value):
        return asdict(value)
    return value


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


class ExperimentDir:
    def __init__(self, *, run_dir: Path, manifest: dict[str, Any]) -> None:
        self.run_dir = run_dir
        self.manifest = manifest

    @property
    def run_id(self) -> str:
        return str(self.manifest.get("run_id") or "")

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "manifest.json"

    @classmethod
    def default_root(cls) -> Path:
        return _REPORT_ROOT

    @classmethod
    def create(
        cls,
        *,
        tag_set_ver: str,
        provider: str,
        model: str,
        seed: int,
        temperature: float,
        n_repeats: int,
        scripts: list[str],
        cache_disabled: bool,
        run_id: str | None = None,
        root: Path | None = None,
    ) -> "ExperimentDir":
        run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        base = root or cls.default_root()
        run_dir = base / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "raw" / "layer_a").mkdir(parents=True, exist_ok=True)
        (run_dir / "raw" / "layer_b").mkdir(parents=True, exist_ok=True)
        (run_dir / "aggregated" / "layer_b").mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": run_id,
            "tag_set_ver": tag_set_ver,
            "provider": provider,
            "model": model,
            "seed": seed,
            "temperature": temperature,
            "n_repeats": n_repeats,
            "cache_disabled": bool(cache_disabled),
            "scripts": list(scripts),
            "started_at": _utc_now_iso(),
            "stages": {
                "ingest": "pending",
                "freeze": "pending",
                "layer_a": "pending",
                "layer_b": "pending",
            },
        }
        _write_json_atomic(run_dir / "manifest.json", manifest)
        return cls(run_dir=run_dir, manifest=manifest)

    @classmethod
    def load(cls, run_id: str, *, root: Path | None = None) -> "ExperimentDir":
        base = root or cls.default_root()
        run_dir = base / run_id
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"manifest not found for run_id={run_id!r}: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return cls(run_dir=run_dir, manifest=manifest)

    def _write_manifest(self) -> None:
        _write_json_atomic(self.manifest_path, self.manifest)

    def update_stage(self, stage: str, status: StageStatus) -> None:
        stages = self.manifest.setdefault("stages", {})
        stages[stage] = status
        self._write_manifest()

    def layer_a_raw_path(self, script_id: str, rep: int) -> Path:
        return self.run_dir / "raw" / "layer_a" / script_id / f"{rep}.json"

    def layer_b_raw_path(self, script_id: str, scope: str, dim: str, rep: int) -> Path:
        return self.run_dir / "raw" / "layer_b" / script_id / scope / dim / f"{rep}.jsonl"

    def save_layer_a_raw(self, script_id: str, rep: int, units: list[Any]) -> None:
        path = self.layer_a_raw_path(script_id, rep)
        serialized = [_as_jsonable(item) for item in units]
        _write_json_atomic(path, serialized)

    def load_layer_a_raw(self, script_id: str, rep: int) -> list[dict[str, Any]]:
        path = self.layer_a_raw_path(script_id, rep)
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def append_layer_b_raw(
        self,
        script_id: str,
        scope: str,
        dim: str,
        rep: int,
        target_id: str,
        value: str,
        meta: dict[str, Any],
    ) -> None:
        path = self.layer_b_raw_path(script_id, scope, dim, rep)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"target_id": target_id, "value": value, **(meta or {})}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()

    def load_layer_b_raw(self, script_id: str, scope: str, dim: str, rep: int) -> list[dict[str, Any]]:
        path = self.layer_b_raw_path(script_id, scope, dim, rep)
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                text_line = line.strip()
                if not text_line:
                    continue
                try:
                    payload = json.loads(text_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
        return rows

    def layer_b_value_map(self, script_id: str, scope: str, dim: str, rep: int) -> dict[str, str]:
        rows = self.load_layer_b_raw(script_id, scope, dim, rep)
        out: dict[str, str] = {}
        for row in rows:
            target_id = str(row.get("target_id") or "").strip()
            if not target_id:
                continue
            out[target_id] = str(row.get("value") or "")
        return out

    def save_aggregated_layer_a(self, reports: Iterable[Any]) -> None:
        payload = [_as_jsonable(report) for report in reports]
        _write_json_atomic(self.run_dir / "aggregated" / "layer_a.json", payload)

    def save_aggregated_layer_b(self, reports: dict[str, Any]) -> None:
        layer_b_dir = self.run_dir / "aggregated" / "layer_b"
        layer_b_dir.mkdir(parents=True, exist_ok=True)
        for dim, report in reports.items():
            _write_json_atomic(layer_b_dir / f"{dim}.json", _as_jsonable(report))

    def save_decision_markdown(self, markdown: str) -> Path:
        path = self.run_dir / "decision.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(markdown, encoding="utf-8")
        tmp_path.replace(path)
        return path

    def is_layer_a_done(self, script_id: str, rep: int) -> bool:
        path = self.layer_a_raw_path(script_id, rep)
        return path.exists() and path.stat().st_size > 2

    def is_layer_b_done(
        self,
        script_id: str,
        scope: str,
        dim: str,
        rep: int,
        *,
        target_count: int | None = None,
    ) -> bool:
        path = self.layer_b_raw_path(script_id, scope, dim, rep)
        if not path.exists() or path.stat().st_size <= 0:
            return False
        if target_count is None:
            return True
        return len(self.load_layer_b_raw(script_id, scope, dim, rep)) >= target_count

    def pending_layer_a(self, scripts: list[str], n_repeats: int) -> list[tuple[str, int]]:
        pending: list[tuple[str, int]] = []
        for script_id in scripts:
            for rep in range(n_repeats):
                if not self.is_layer_a_done(script_id, rep):
                    pending.append((script_id, rep))
        return pending

    def pending_layer_b(
        self,
        scripts: list[str],
        scopes_dims: list[tuple[str, str]],
        n_repeats: int,
        *,
        target_counts: dict[tuple[str, str, str], int] | None = None,
    ) -> list[tuple[str, str, str, int]]:
        pending: list[tuple[str, str, str, int]] = []
        target_counts = target_counts or {}
        for script_id in scripts:
            for scope, dim in scopes_dims:
                expected = target_counts.get((script_id, scope, dim))
                for rep in range(n_repeats):
                    if not self.is_layer_b_done(script_id, scope, dim, rep, target_count=expected):
                        pending.append((script_id, scope, dim, rep))
        return pending
