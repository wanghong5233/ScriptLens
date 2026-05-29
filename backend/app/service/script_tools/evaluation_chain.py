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

"""评估层封装：六维评分 + 改写候选（docs/08-evaluation-framework.md）。

该模块不重新评分，只把上游评分结果整理成 ReportPayload.evaluation 字段。
合规审核（compliance）独立成 ReportPayload.compliance 字段，不在六维评分里。
"""

from __future__ import annotations

from typing import Dict, List, Optional


# 六维 + 短剧场景化文案（详见 docs/08-evaluation-framework.md §3）
_DIM_LABELS = {
    "story": "故事力",
    "character": "人物力",
    "concept": "题材力",
    "emotion": "情感力",
    "pacing": "叙事力",
}

def build_evaluation_payload(
    *,
    scorecard: List[dict],
    evidence_refs: List[dict],
    risk_flags: List[str],
    max_rewrite_seeds: int = 3,
) -> dict:
    return {
        "dimensions": [
            {
                "key": item["dimension"],
                "label": _DIM_LABELS.get(item["dimension"], item["dimension"]),
                "score": item.get("score"),
                "level": item.get("level"),
                "reason": item.get("reason") or "",
                "evidence_ref_ids": list(item.get("evidence_ref_ids") or []),
            }
            for item in scorecard
        ],
        "risk_flags": list(risk_flags or []),
        "rewrite_seeds": _derive_rewrite_seed_dicts(
            scorecard=scorecard,
            evidence_refs=evidence_refs,
            max_seeds=max_rewrite_seeds,
        ),
    }


def _derive_rewrite_seed_dicts(
    *,
    scorecard: List[dict],
    evidence_refs: List[dict],
    max_seeds: int,
) -> List[dict]:
    evi_by_id: Dict[str, dict] = {str(ref.get("id")): ref for ref in evidence_refs}

    candidates: List[tuple[int, dict]] = []
    for item in scorecard:
        score: Optional[int] = item.get("score")
        if score is None or score >= 7:
            continue
        if not item.get("evidence_ref_ids"):
            continue
        candidates.append((score, item))

    candidates.sort(key=lambda t: t[0])

    seeds: List[dict] = []
    used_scenes: set[str] = set()
    for _score_key, item in candidates:
        if len(seeds) >= max_seeds:
            break
        evi = _first_existing_evidence(item.get("evidence_ref_ids") or [], evi_by_id)
        if evi is None:
            continue
        scene_id = str(evi.get("scene_id") or "")
        if not scene_id or scene_id in used_scenes:
            continue
        used_scenes.add(scene_id)
        seeds.append(
            {
                "dimension": item["dimension"],
                "scene_id": scene_id,
                "scene_label": evi.get("scene_label") or evi.get("scene_no") or "",
                "issue": _first_sentence(str(item.get("reason") or "")),
                "severity": _severity(item),
                "evidence_ref_id": evi.get("id"),
            }
        )
    return seeds


def _first_existing_evidence(ref_ids: List[str], evi_by_id: Dict[str, dict]) -> Optional[dict]:
    for ref_id in ref_ids:
        evi = evi_by_id.get(str(ref_id))
        if evi is not None:
            return evi
    return None


def _severity(item: dict) -> str:
    score = item.get("score")
    if score is not None and score < 3:
        return "high"
    if score is not None and score < 5:
        return "medium"
    return "low"


def _first_sentence(text: str, *, max_len: int = 80) -> str:
    if not text:
        return ""
    chunk = text.strip()
    for sep in ("\n", "。", "；", "！", "?"):
        if sep in chunk:
            chunk = chunk.split(sep, 1)[0]
            break
    chunk = chunk.strip()
    if len(chunk) > max_len:
        chunk = chunk[: max_len - 1] + "…"
    return chunk
