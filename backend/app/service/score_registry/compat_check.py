# ============================================================
# DEPRECATED — release/v1-mvp (2026-05-29)
# ============================================================
#
# 本文件属于已废弃的「整剧抽情节打标签 → rubric/signal/aggregator
# 评分」流水线（Batch3 体系）。release/v1-mvp 已切回 self-contained
# 6 维规则评分，主流程入口：
#   - service/script_tools/dimension_scorer.py
#   - service/script_report_service.py（generate_report）
# 当前已不再调用本模块任何函数。
#
# 保留原因：避免 git history 大面积污染、便于必要时回收实现细节。
# 清理时机：下次 cleanup PR 统一删除（含本文件、其测试、CLI 入口
# 与 score_registry/rubric_sets/v3.yaml 等配套资产）。
#
# 不要在本文件内再做任何功能性修改。如需新评分能力，请扩展
# dimension_scorer.py。
# ============================================================

from __future__ import annotations

from dataclasses import asdict, dataclass

from service.score_registry.loader import RubricConfig, SignalConfig, load_rubric


@dataclass(frozen=True)
class CompatIssue:
    kind: str
    dimension: str = ""
    signal: str = ""
    bundle: str = ""
    before: str = ""
    after: str = ""
    detail: str = ""


@dataclass(frozen=True)
class CompatResult:
    compatible: bool
    mode: str
    allow_breaking: bool
    issues: tuple[CompatIssue, ...]

    def to_dict(self) -> dict:
        return {
            "compatible": self.compatible,
            "mode": self.mode,
            "allow_breaking": self.allow_breaking,
            "issues": [asdict(issue) for issue in self.issues],
        }


def _allowed_issue_kinds(mode: str) -> set[str]:
    normalized = (mode or "BACKWARD").strip().upper()
    if normalized == "NONE":
        return {
            "add_dim",
            "remove_dim",
            "add_signal",
            "remove_signal",
            "relabel_signal",
            "add_bundle",
            "remove_bundle",
            "weight_change",
            "source_change",
            "primary_change",
            "base_weight_change",
            "tier_cut_change",
        }
    if normalized == "FORWARD":
        return {
            "remove_dim",
            "remove_signal",
            "remove_bundle",
            "weight_change",
            "source_change",
            "primary_change",
            "base_weight_change",
            "tier_cut_change",
        }
    if normalized == "FULL":
        return {
            "add_dim",
            "remove_dim",
            "add_signal",
            "remove_signal",
            "add_bundle",
            "remove_bundle",
            "weight_change",
            "source_change",
            "primary_change",
            "base_weight_change",
            "tier_cut_change",
        }
    return {
        "add_dim",
        "add_signal",
        "add_bundle",
        "weight_change",
        "source_change",
        "primary_change",
        "base_weight_change",
        "tier_cut_change",
    }


def _signal_map(signals: tuple[SignalConfig, ...]) -> dict[str, SignalConfig]:
    return {signal.id: signal for signal in signals}


def compare_rubrics(
    baseline: RubricConfig,
    candidate: RubricConfig,
    *,
    mode: str = "BACKWARD",
    allow_breaking: bool = False,
) -> CompatResult:
    baseline_dims = {dim.id: dim for dim in baseline.dimensions}
    candidate_dims = {dim.id: dim for dim in candidate.dimensions}
    issues: list[CompatIssue] = []

    baseline_dim_ids = set(baseline_dims.keys())
    candidate_dim_ids = set(candidate_dims.keys())

    for dim_id in sorted(candidate_dim_ids - baseline_dim_ids):
        issues.append(
            CompatIssue(
                kind="add_dim",
                dimension=dim_id,
                detail="dimension added in candidate rubric",
            )
        )
    for dim_id in sorted(baseline_dim_ids - candidate_dim_ids):
        issues.append(
            CompatIssue(
                kind="remove_dim",
                dimension=dim_id,
                detail="dimension removed from baseline rubric",
            )
        )

    for dim_id in sorted(baseline_dim_ids & candidate_dim_ids):
        left_dim = baseline_dims[dim_id]
        right_dim = candidate_dims[dim_id]
        left_signal_map = _signal_map(left_dim.signals)
        right_signal_map = _signal_map(right_dim.signals)

        removed = sorted(set(left_signal_map.keys()) - set(right_signal_map.keys()))
        added = sorted(set(right_signal_map.keys()) - set(left_signal_map.keys()))

        if len(removed) == 1 and len(added) == 1:
            issues.append(
                CompatIssue(
                    kind="relabel_signal",
                    dimension=dim_id,
                    signal=removed[0],
                    before=removed[0],
                    after=added[0],
                    detail="single signal replacement detected",
                )
            )
        else:
            for signal in removed:
                issues.append(
                    CompatIssue(
                        kind="remove_signal",
                        dimension=dim_id,
                        signal=signal,
                        detail="signal removed from dimension",
                    )
                )
            for signal in added:
                issues.append(
                    CompatIssue(
                        kind="add_signal",
                        dimension=dim_id,
                        signal=signal,
                        detail="signal added to dimension",
                    )
                )

        for signal in sorted(set(left_signal_map.keys()) & set(right_signal_map.keys())):
            left_signal = left_signal_map[signal]
            right_signal = right_signal_map[signal]
            if round(float(left_signal.weight_in_dim), 6) != round(float(right_signal.weight_in_dim), 6):
                issues.append(
                    CompatIssue(
                        kind="weight_change",
                        dimension=dim_id,
                        signal=signal,
                        before=str(left_signal.weight_in_dim),
                        after=str(right_signal.weight_in_dim),
                        detail="weight_in_dim changed",
                    )
                )
            if left_signal.source != right_signal.source:
                issues.append(
                    CompatIssue(
                        kind="source_change",
                        dimension=dim_id,
                        signal=signal,
                        before=left_signal.source,
                        after=right_signal.source,
                        detail="signal source changed",
                    )
                )
            if bool(left_signal.primary) != bool(right_signal.primary):
                issues.append(
                    CompatIssue(
                        kind="primary_change",
                        dimension=dim_id,
                        signal=signal,
                        before=str(left_signal.primary),
                        after=str(right_signal.primary),
                        detail="signal primary flag changed",
                    )
                )

        left_bw = baseline.base_weight.get(dim_id)
        right_bw = candidate.base_weight.get(dim_id)
        if left_bw is not None and right_bw is not None:
            if round(float(left_bw), 6) != round(float(right_bw), 6):
                issues.append(
                    CompatIssue(
                        kind="base_weight_change",
                        dimension=dim_id,
                        before=str(left_bw),
                        after=str(right_bw),
                        detail="dimension base_weight changed",
                    )
                )

        left_cut = baseline.tier_cuts.get("default", {}).get(dim_id, {})
        right_cut = candidate.tier_cuts.get("default", {}).get(dim_id, {})
        if left_cut != right_cut:
            issues.append(
                CompatIssue(
                    kind="tier_cut_change",
                    dimension=dim_id,
                    before=str(left_cut),
                    after=str(right_cut),
                    detail="default tier cuts changed",
                )
            )

    baseline_bundles = {bundle.id for bundle in baseline.llm_bundles}
    candidate_bundles = {bundle.id for bundle in candidate.llm_bundles}
    for bundle_id in sorted(candidate_bundles - baseline_bundles):
        issues.append(
            CompatIssue(kind="add_bundle", bundle=bundle_id, detail="llm bundle added")
        )
    for bundle_id in sorted(baseline_bundles - candidate_bundles):
        issues.append(
            CompatIssue(kind="remove_bundle", bundle=bundle_id, detail="llm bundle removed")
        )

    allowed_kinds = _allowed_issue_kinds(mode)
    blocked_issues = [issue for issue in issues if issue.kind not in allowed_kinds]
    compatible = allow_breaking or not blocked_issues
    return CompatResult(
        compatible=compatible,
        mode=(mode or "BACKWARD").strip().upper(),
        allow_breaking=allow_breaking,
        issues=tuple(issues),
    )


def check_rubric_compatibility(
    baseline_rubric_id: str,
    candidate_rubric_id: str,
    *,
    mode: str = "BACKWARD",
    breaking: bool | None = None,
) -> CompatResult:
    baseline = load_rubric(baseline_rubric_id)
    candidate = load_rubric(candidate_rubric_id)
    allow_breaking = bool(candidate.breaking) if breaking is None else bool(breaking)
    return compare_rubrics(
        baseline,
        candidate,
        mode=mode,
        allow_breaking=allow_breaking,
    )
