from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_PROJECT_DECISION_DOC = _PROJECT_ROOT / "docs" / "script_tag_stability_decision_20260527.md"


def _bucket_from_wilson(wilson_lower: float) -> str:
    if wilson_lower >= 0.85:
        return "可进入跨模态共享候选"
    if wilson_lower >= 0.7:
        return "可用但建议聚合"
    if wilson_lower >= 0.5:
        return "需要收紧 prompt 后复测"
    return "暂不进共享内核"


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _build_markdown(
    *,
    manifest: dict[str, Any],
    layer_a_rows: list[dict[str, Any]],
    layer_b_rows: list[dict[str, Any]],
) -> str:
    lines: list[str] = [
        "# 剧本标签稳定性决策",
        "",
        "## 1. 实验设置",
        "",
        f"- provider: `{manifest.get('provider', '')}`",
        f"- model: `{manifest.get('model', '')}`",
        f"- tag_set_ver: `{manifest.get('tag_set_ver', '')}`",
        f"- seed: `{manifest.get('seed', '')}`",
        f"- temperature: `{manifest.get('temperature', '')}`",
        f"- n_repeats: `{manifest.get('n_repeats', '')}`",
        f"- cache_disabled: `{manifest.get('cache_disabled', '')}`",
        f"- started_at: `{manifest.get('started_at', '')}`",
        "",
        "## 2. 样本概况",
        "",
        f"- scripts: `{len(manifest.get('scripts', []))}`",
        "",
        "## 3. 第一层结论按脚本",
        "",
        "| script_id | unit_count_cv | window_diff_mean | boundary_similarity_mean | verdict |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in sorted(layer_a_rows, key=lambda item: str(item.get("script_id", ""))):
        lines.append(
            f"| `{row.get('script_id', '')}` | {float(row.get('unit_count_cv', 0.0)):.3f} | "
            f"{float(row.get('window_diff_mean', 0.0)):.3f} | {float(row.get('boundary_similarity_mean', 0.0)):.3f} | "
            f"{row.get('verdict', '')} |"
        )

    lines.extend(
        [
            "",
            "## 4. 第二层结论按字段",
            "",
            "| 字段 | avg agreement | stable | Wilson 95% lower | 决策桶 |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    bucket_to_dims: dict[str, list[str]] = {
        "可进入跨模态共享候选": [],
        "可用但建议聚合": [],
        "需要收紧 prompt 后复测": [],
        "暂不进共享内核": [],
    }
    for row in sorted(layer_b_rows, key=lambda item: str(item.get("dim", ""))):
        dim = str(row.get("dim", ""))
        n_samples = int(row.get("n_samples", 0) or 0)
        stable_count = int(row.get("stable_count", 0) or 0)
        wilson_lower = float(row.get("wilson_lower", 0.0) or 0.0)
        bucket = _bucket_from_wilson(wilson_lower)
        bucket_to_dims.setdefault(bucket, []).append(dim)
        stable = f"{stable_count}/{n_samples}" if n_samples > 0 else "0/0"
        lines.append(
            f"| `{dim}` | {float(row.get('par', 0.0)):.3f} | {stable} | {wilson_lower:.3f} | {bucket} |"
        )

    lines.extend(["", "## 5. 4 桶分组列表", ""])
    for bucket in [
        "可进入跨模态共享候选",
        "可用但建议聚合",
        "需要收紧 prompt 后复测",
        "暂不进共享内核",
    ]:
        lines.append(f"### {bucket}")
        dims = bucket_to_dims.get(bucket, [])
        if not dims:
            lines.append("- （空）")
        else:
            for dim in dims:
                lines.append(f"- `{dim}`")
        lines.append("")

    lines.extend(
        [
            "## 6. 主要漂移与修复建议",
            "",
        ]
    )
    unstable_dims = [str(row.get("dim", "")) for row in layer_b_rows if row.get("verdict") in {"offline", "fix"}]
    if unstable_dims:
        lines.append(f"- 漂移主要集中在：{', '.join(f'`{dim}`' for dim in unstable_dims)}。")
        lines.append("- 修复顺序建议：先收紧定义边界（枚举互斥条件），再复测 Wilson 下界。")
    else:
        lines.append("- 当前维度未发现明显漂移，建议保持 prompt 与 tag_set 固定，按批次抽检。")
    lines.append("")
    return "\n".join(lines)


def generate_script_tag_stability_decision(*, run_dir: Path) -> dict[str, str]:
    run_dir = run_dir.resolve()
    manifest = _load_json(run_dir / "manifest.json", {})
    layer_a_rows = _load_json(run_dir / "aggregated" / "layer_a.json", [])
    layer_b_rows: list[dict[str, Any]] = []
    layer_b_dir = run_dir / "aggregated" / "layer_b"
    if layer_b_dir.exists():
        for path in sorted(layer_b_dir.glob("*.json")):
            payload = _load_json(path, {})
            if isinstance(payload, dict):
                payload.setdefault("dim", path.stem)
                layer_b_rows.append(payload)

    markdown = _build_markdown(
        manifest=manifest if isinstance(manifest, dict) else {},
        layer_a_rows=layer_a_rows if isinstance(layer_a_rows, list) else [],
        layer_b_rows=layer_b_rows,
    )
    run_decision_path = run_dir / "decision.md"
    run_decision_path.parent.mkdir(parents=True, exist_ok=True)
    run_decision_path.write_text(markdown, encoding="utf-8")

    _PROJECT_DECISION_DOC.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(run_decision_path, _PROJECT_DECISION_DOC)
    return {
        "run_decision_md": str(run_decision_path),
        "project_decision_md": str(_PROJECT_DECISION_DOC),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate script tag stability decision markdown from run directory.")
    parser.add_argument("--run-dir", required=True, help="path to eval/reports/script_stability_v2/<run_id>")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = generate_script_tag_stability_decision(run_dir=Path(args.run_dir))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
