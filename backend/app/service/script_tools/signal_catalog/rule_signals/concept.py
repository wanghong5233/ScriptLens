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

from service.script_tools.risk_terms import HOOK_KEYWORDS, MAINSTREAM_GENRES
from service.script_tools.signal_catalog import SignalContext, SignalValue, register_signal


@register_signal("drama_tags_confidence", scope="script", source="rule", primary_dim="concept")
def compute_drama_tags_confidence(ctx: SignalContext) -> SignalValue:
    drama_rows = [row for row in ctx.script_tags if str(row.get("dim") or "") == "drama_tags"]
    confidence_values = []
    for row in drama_rows:
        raw = row.get("confidence")
        if raw is None:
            continue
        try:
            confidence_values.append(float(raw))
        except (TypeError, ValueError):
            continue
    if confidence_values:
        avg_conf = sum(confidence_values) / len(confidence_values)
        score = round(min(10.0, max(0.0, avg_conf * 10.0)), 2)
    elif ctx.drama_tags:
        avg_conf = 0.6
        score = 6.0
    else:
        avg_conf = 0.2
        score = 2.0
    return SignalValue(
        key="drama_tags_confidence",
        value={"avg_confidence": round(avg_conf, 4), "tag_count": len(ctx.drama_tags)},
        score=score,
        source="rule",
        confidence=0.85,
        evidence_refs=[{"dim": "drama_tags", "value": tag} for tag in ctx.drama_tags[:3]],
    )


@register_signal("genre_market_fit", scope="script", source="rule", primary_dim="concept")
def compute_genre_market_fit(ctx: SignalContext) -> SignalValue:
    if not ctx.drama_tags:
        return SignalValue(
            key="genre_market_fit",
            value={"mainstream_ratio": 0.0, "matched": []},
            score=2.0,
            source="rule",
            confidence=0.4,
        )
    matched = [tag for tag in ctx.drama_tags if any(genre in tag for genre in MAINSTREAM_GENRES)]
    ratio = len(matched) / max(len(ctx.drama_tags), 1)
    score = round(min(10.0, max(1.0, ratio * 10.0)), 2)
    return SignalValue(
        key="genre_market_fit",
        value={"mainstream_ratio": round(ratio, 4), "matched": matched},
        score=score,
        source="rule",
        confidence=0.8,
    )


@register_signal("early_genre_signal", scope="script", source="rule", primary_dim="concept")
def compute_early_genre_signal(ctx: SignalContext) -> SignalValue:
    if not ctx.plot_units:
        return SignalValue(
            key="early_genre_signal",
            value={"hit": False, "window_units": 0},
            score=2.0,
            source="rule",
            confidence=0.3,
        )
    total_units = len(ctx.plot_units)
    window_size = max(1, min(total_units, int(total_units * 0.2) or 1))
    candidate_units = ctx.plot_units[:window_size]
    hit_unit_id = ""
    for unit in candidate_units:
        unit_id = str(unit.get("id") or "")
        plot_hook = ctx.unit_value(unit_id, "plot_hook", default="none")
        conflict_type = ctx.unit_value(unit_id, "conflict_type", default="none")
        summary = str(unit.get("summary") or "")
        has_keyword = any(keyword in summary for keyword in HOOK_KEYWORDS)
        if plot_hook != "none" or conflict_type != "none" or has_keyword:
            hit_unit_id = unit_id
            break
    score = 9.0 if hit_unit_id else 3.5
    evidence = [{"plot_unit_id": hit_unit_id}] if hit_unit_id else []
    return SignalValue(
        key="early_genre_signal",
        value={"hit": bool(hit_unit_id), "window_units": window_size},
        score=score,
        source="rule",
        confidence=0.75,
        evidence_refs=evidence,
    )
