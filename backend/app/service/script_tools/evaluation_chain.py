"""评估层封装：5 维评分 + 风险 + 改写候选。

该模块不重新评分，只把现有评分结果整理成 ReportPayload.v3 的 evaluation 字段。
"""

from __future__ import annotations

from typing import Dict, List, Optional


_DIM_LABELS = {
    "opening_hook": "开场抓人",
    "reward_density": "看点密度",
    "motivation": "动机成立",
    "pacing": "节奏清楚",
    "risk": "审核风险",
}

_LOW_RISK_LEVELS = {"high_risk", "medium_risk", "major"}


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

    candidates: List[tuple[int, int, dict]] = []
    for item in scorecard:
        level = str(item.get("level") or "")
        score: Optional[int] = item.get("score")
        is_risk_flag = level in _LOW_RISK_LEVELS
        score_low = score is not None and score < 7
        if not (is_risk_flag or score_low):
            continue
        if not item.get("evidence_ref_ids"):
            continue
        risk_key = 0 if is_risk_flag else 1
        score_key = score if score is not None else 99
        candidates.append((risk_key, score_key, item))

    candidates.sort(key=lambda t: (t[0], t[1]))

    seeds: List[dict] = []
    used_scenes: set[str] = set()
    for _risk_key, _score_key, item in candidates:
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
    level = str(item.get("level") or "")
    score = item.get("score")
    if level in {"high_risk", "major"}:
        return "high"
    if level == "medium_risk" or (score is not None and score < 5):
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
