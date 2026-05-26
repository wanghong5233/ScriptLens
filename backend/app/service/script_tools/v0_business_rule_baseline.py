from __future__ import annotations

from typing import Iterable


_CONFLICT_TO_BUCKET = {
    "status_gap": "relationship_power",
    "family_conflict": "relationship_power",
    "romantic_conflict": "relationship_power",
    "misunderstanding": "relationship_power",
    "moral_judgement": "relationship_power",
    "survival_crisis": "survival",
    "workplace_conflict": "workplace",
    "revenge": "relationship_power",
    "none": "none",
}

_PAYOFF_TO_BUCKET = {
    "face_slapping": "face_slap_counter",
    "counterattack": "face_slap_counter",
    "reveal_power": "power_reveal",
    "romantic_payoff": "emotional_romance",
    "justice_served": "justice_punishment",
    "cliffhanger": "suspense_cliffhanger",
    "comic_relief": "comedy",
    "none": "none",
}

_EMOTION_TO_BUCKET = {
    "humiliation": "anger_humiliation",
    "anger": "anger_humiliation",
    "curiosity": "curiosity_suspense",
    "fear": "fear_crisis",
    "regret": "warm_regret_pity",
    "pity": "warm_regret_pity",
    "tenderness": "warm_regret_pity",
    "desire": "desire_jealousy",
    "jealousy": "desire_jealousy",
    "none": "neutral",
}


def derive_business_tags(plot_values: dict[str, str]) -> dict[str, str]:
    conflict_type = (plot_values.get("conflict_type") or "none").strip()
    payoff_type = (plot_values.get("payoff_type") or "none").strip()
    emotional_driver = (plot_values.get("emotional_driver") or "none").strip()
    plot_hook = (plot_values.get("plot_hook") or "none").strip()

    business_conflict_bucket = _CONFLICT_TO_BUCKET.get(conflict_type, "none")
    business_payoff_bucket = _PAYOFF_TO_BUCKET.get(payoff_type, "none")
    business_emotion_bucket = _EMOTION_TO_BUCKET.get(emotional_driver, "neutral")

    if business_conflict_bucket == "survival":
        content = "survival_suspense"
    elif business_conflict_bucket == "workplace":
        content = "workplace_counter"
    elif business_payoff_bucket in {"face_slap_counter", "power_reveal", "justice_punishment"}:
        content = "power_payoff"
    elif business_payoff_bucket in {"emotional_romance", "rescue_protection"}:
        content = "relationship_payoff"
    elif business_payoff_bucket in {"comedy"}:
        content = "comedy_light"
    elif plot_hook in {"conflict_escalation", "secret_exposure", "betrayal"}:
        content = "survival_suspense"
    else:
        content = "unclear"

    return {
        "business_content_archetype": content,
        "business_conflict_bucket": business_conflict_bucket,
        "business_payoff_bucket": business_payoff_bucket,
        "business_emotion_bucket": business_emotion_bucket,
    }


def _cohen_kappa(a: Iterable[str], b: Iterable[str]) -> float:
    left = list(a)
    right = list(b)
    if not left or not right or len(left) != len(right):
        return 0.0
    try:
        from sklearn.metrics import cohen_kappa_score

        return float(cohen_kappa_score(left, right))
    except Exception:
        same = sum(1 for x, y in zip(left, right) if x == y)
        return same / len(left)


def compare_llm_vs_rule(
    llm_values_by_target: dict[str, dict[str, str]],
    rule_values_by_target: dict[str, dict[str, str]],
) -> dict[str, dict[str, float]]:
    dims = (
        "business_content_archetype",
        "business_conflict_bucket",
        "business_payoff_bucket",
        "business_emotion_bucket",
    )
    stats: dict[str, dict[str, float]] = {}
    target_ids = sorted(set(llm_values_by_target.keys()) & set(rule_values_by_target.keys()))
    for dim in dims:
        llm_vec: list[str] = []
        rule_vec: list[str] = []
        for tid in target_ids:
            llm_val = (llm_values_by_target.get(tid) or {}).get(dim)
            rule_val = (rule_values_by_target.get(tid) or {}).get(dim)
            if not llm_val or not rule_val:
                continue
            llm_vec.append(llm_val)
            rule_vec.append(rule_val)
        if not llm_vec:
            stats[dim] = {"n": 0.0, "par": 0.0, "kappa": 0.0}
            continue
        same = sum(1 for x, y in zip(llm_vec, rule_vec) if x == y)
        par = same / len(llm_vec)
        stats[dim] = {"n": float(len(llm_vec)), "par": float(par), "kappa": float(_cohen_kappa(llm_vec, rule_vec))}
    return stats

