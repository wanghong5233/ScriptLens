from __future__ import annotations

from dataclasses import asdict, dataclass

from service.tag_registry.loader import TagSetConfig, load_tag_set


@dataclass(frozen=True)
class CompatIssue:
    kind: str  # add_dim | remove_dim | add_value | remove_value | relabel | parent_change
    dim: str
    scope_before: str | None = None
    scope_after: str | None = None
    value_before: str | None = None
    value_after: str | None = None
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
            "issues": [asdict(x) for x in self.issues],
        }


def _allowed_issue_kinds(mode: str) -> set[str]:
    normalized = (mode or "BACKWARD").strip().upper()
    if normalized == "NONE":
        return {"add_dim", "remove_dim", "add_value", "remove_value", "relabel", "parent_change"}
    if normalized == "FORWARD":
        return {"remove_dim", "remove_value"}
    if normalized == "FULL":
        return {"add_dim", "add_value", "remove_dim", "remove_value"}
    # BACKWARD default
    return {"add_dim", "add_value"}


def compare_tag_sets(
    baseline: TagSetConfig,
    candidate: TagSetConfig,
    *,
    mode: str = "BACKWARD",
    allow_breaking: bool = False,
) -> CompatResult:
    baseline_dims = {d.dim: d for dims in baseline.scope_to_dims.values() for d in dims}
    candidate_dims = {d.dim: d for dims in candidate.scope_to_dims.values() for d in dims}
    issues: list[CompatIssue] = []

    baseline_dim_set = set(baseline_dims.keys())
    candidate_dim_set = set(candidate_dims.keys())

    for dim in sorted(candidate_dim_set - baseline_dim_set):
        d = candidate_dims[dim]
        issues.append(
            CompatIssue(
                kind="add_dim",
                dim=dim,
                scope_before=None,
                scope_after=d.scope,
                detail=f"new dim added in candidate scope={d.scope}",
            )
        )
    for dim in sorted(baseline_dim_set - candidate_dim_set):
        d = baseline_dims[dim]
        issues.append(
            CompatIssue(
                kind="remove_dim",
                dim=dim,
                scope_before=d.scope,
                scope_after=None,
                detail=f"dim removed from baseline scope={d.scope}",
            )
        )

    for dim in sorted(baseline_dim_set & candidate_dim_set):
        left = baseline_dims[dim]
        right = candidate_dims[dim]
        if left.scope != right.scope:
            issues.append(
                CompatIssue(
                    kind="parent_change",
                    dim=dim,
                    scope_before=left.scope,
                    scope_after=right.scope,
                    detail=f"dim moved scope: {left.scope} -> {right.scope}",
                )
            )

        removed = sorted(set(left.values) - set(right.values))
        added = sorted(set(right.values) - set(left.values))

        if len(removed) == 1 and len(added) == 1:
            issues.append(
                CompatIssue(
                    kind="relabel",
                    dim=dim,
                    scope_before=left.scope,
                    scope_after=right.scope,
                    value_before=removed[0],
                    value_after=added[0],
                    detail="single-value swap detected",
                )
            )
            continue

        for value in removed:
            issues.append(
                CompatIssue(
                    kind="remove_value",
                    dim=dim,
                    scope_before=left.scope,
                    scope_after=right.scope,
                    value_before=value,
                    detail="enum value removed",
                )
            )
        for value in added:
            issues.append(
                CompatIssue(
                    kind="add_value",
                    dim=dim,
                    scope_before=left.scope,
                    scope_after=right.scope,
                    value_after=value,
                    detail="enum value added",
                )
            )

    allowed = _allowed_issue_kinds(mode)
    blocked_issues = [issue for issue in issues if issue.kind not in allowed]
    compatible = allow_breaking or not blocked_issues
    return CompatResult(
        compatible=compatible,
        mode=(mode or "BACKWARD").strip().upper(),
        allow_breaking=allow_breaking,
        issues=tuple(issues),
    )


def check_tagset_compatibility(
    baseline_tag_set_ver: str,
    candidate_tag_set_ver: str,
    *,
    mode: str = "BACKWARD",
    breaking: bool | None = None,
) -> CompatResult:
    baseline = load_tag_set(baseline_tag_set_ver)
    candidate = load_tag_set(candidate_tag_set_ver)
    allow_breaking = bool(candidate.breaking) if breaking is None else bool(breaking)
    return compare_tag_sets(
        baseline,
        candidate,
        mode=mode,
        allow_breaking=allow_breaking,
    )

