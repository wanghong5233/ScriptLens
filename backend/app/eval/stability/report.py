from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from eval.stability.metrics import (
    cohen_kappa_mean,
    confusion_matrix_dict,
    cosine_similarity_enum,
    krippendorff_alpha,
    macro_f1_against_gold,
    majority_vote,
    pairwise_agreement_rate,
)
from eval.stability.runner import RunResult


@dataclass
class DimStabilityReport:
    dim: str
    intra_alpha: float
    inter_alpha: float
    kappa_mean: float
    par: float
    cosine: float
    macro_f1: Optional[float]
    verdict: str  # online | fix | offline
    unstable_values: list[str] = field(default_factory=list)
    genre_sensitive: list[str] = field(default_factory=list)
    confusion: dict | None = None


def _verdict(intra: float, inter: float, kappa: float, par: float) -> str:
    if intra >= 0.7 and inter >= 0.6 and kappa >= 0.6 and par >= 0.6:
        return "online"
    if intra < 0.4 or inter < 0.3 or kappa < 0.4 or par < 0.4:
        return "offline"
    return "fix"


def _unstable_values(matrix: list[list[str]]) -> list[str]:
    if not matrix:
        return []
    n = len(matrix[0])
    unstable: set[str] = set()
    for col in range(n):
        values = {row[col] for row in matrix if len(row) > col and row[col] != ""}
        if len(values) > 1:
            unstable.update(values)
    return sorted(unstable)


def aggregate(
    run_results: dict[str, RunResult],
    *,
    gold: dict[str, list[str]] | None = None,
) -> dict[str, DimStabilityReport]:
    reports: dict[str, DimStabilityReport] = {}
    gold = gold or {}
    for dim, rr in run_results.items():
        intra = rr.matrix("intra")
        inter = rr.matrix("inter")

        intra_alpha = krippendorff_alpha(intra)
        inter_alpha = krippendorff_alpha(inter)
        kappa = cohen_kappa_mean(intra)
        par = pairwise_agreement_rate(intra)
        cosine = cosine_similarity_enum(intra)

        mv = majority_vote(intra)
        gold_vec = gold.get(dim)
        macro_f1 = None
        confusion = None
        if gold_vec is not None and len(gold_vec) == len(mv):
            macro_f1 = macro_f1_against_gold(mv, gold_vec)
            confusion = confusion_matrix_dict(mv, gold_vec)

        reports[dim] = DimStabilityReport(
            dim=dim,
            intra_alpha=intra_alpha,
            inter_alpha=inter_alpha,
            kappa_mean=kappa,
            par=par,
            cosine=cosine,
            macro_f1=macro_f1,
            verdict=_verdict(intra_alpha, inter_alpha, kappa, par),
            unstable_values=_unstable_values(intra),
            genre_sensitive=[],
            confusion=confusion,
        )
    return reports


def write_markdown(
    reports: dict[str, DimStabilityReport],
    path: str,
    *,
    tag_set_ver: str,
    split: str,
) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# Stability Report ({tag_set_ver} / {split})",
        "",
        "| dim | intra_alpha | inter_alpha | kappa | PAR | cosine | macro_f1 | verdict |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for dim in sorted(reports.keys()):
        r = reports[dim]
        f1_str = f"{r.macro_f1:.3f}" if r.macro_f1 is not None else "-"
        lines.append(
            f"| {dim} | {r.intra_alpha:.3f} | {r.inter_alpha:.3f} | {r.kappa_mean:.3f} | "
            f"{r.par:.3f} | {r.cosine:.3f} | {f1_str} | {r.verdict} |"
        )

    lines.append("")
    lines.append("## Unstable Values")
    for dim in sorted(reports.keys()):
        unstable = reports[dim].unstable_values
        if unstable:
            lines.append(f"- `{dim}`: {', '.join(unstable)}")

    p.write_text("\n".join(lines), encoding="utf-8")


def write_per_dim_json(reports: dict[str, DimStabilityReport], dir_path: str) -> None:
    root = Path(dir_path)
    root.mkdir(parents=True, exist_ok=True)
    for dim, report in reports.items():
        payload = asdict(report)
        (root / f"{dim}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _cohen_kappa_pair(left: list[str], right: list[str]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    try:
        from sklearn.metrics import cohen_kappa_score

        return float(cohen_kappa_score(left, right))
    except Exception:
        same = sum(1 for a, b in zip(left, right) if a == b)
        return same / len(left)


def write_baseline_comparison(
    reports: dict[str, DimStabilityReport],
    rule_baseline_outputs: dict[str, dict[str, dict[str, str]]],
    *,
    path: str | None = None,
) -> str:
    """Build markdown appendix for `LLM vs rule baseline` comparisons.

    Expected `rule_baseline_outputs` shape:
    {
      "<dim>": {
        "llm": {"target_id": "value", ...},
        "rule": {"target_id": "value", ...}
      }
    }
    """
    lines = [
        "## LLM vs Rule Baseline",
        "",
        "| dim | n | PAR | kappa | llm_verdict |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for dim in sorted(rule_baseline_outputs.keys()):
        payload = rule_baseline_outputs.get(dim) or {}
        llm_map = payload.get("llm") or {}
        rule_map = payload.get("rule") or {}
        target_ids = sorted(set(llm_map.keys()) & set(rule_map.keys()))
        llm_vec = [str(llm_map[t]) for t in target_ids if str(llm_map[t])]
        rule_vec = [str(rule_map[t]) for t in target_ids if str(rule_map[t])]
        # align after empty-value filter
        if len(llm_vec) != len(rule_vec):
            pair_vec = [(str(llm_map[t]), str(rule_map[t])) for t in target_ids if str(llm_map[t]) and str(rule_map[t])]
            llm_vec = [x for x, _ in pair_vec]
            rule_vec = [y for _, y in pair_vec]

        n = len(llm_vec)
        if n == 0:
            par = 0.0
            kappa = 0.0
        else:
            par = sum(1 for a, b in zip(llm_vec, rule_vec) if a == b) / n
            kappa = _cohen_kappa_pair(llm_vec, rule_vec)
        verdict = reports.get(dim).verdict if dim in reports else "-"
        lines.append(f"| {dim} | {n} | {par:.3f} | {kappa:.3f} | {verdict} |")

    markdown = "\n".join(lines)
    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(markdown, encoding="utf-8")
    return markdown
