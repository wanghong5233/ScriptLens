from service.script_tools.v0_business_rule_baseline import compare_llm_vs_rule, derive_business_tags


def test_derive_business_tags() -> None:
    values = derive_business_tags(
        {
            "plot_hook": "identity_reveal",
            "conflict_type": "status_gap",
            "payoff_type": "counterattack",
            "emotional_driver": "humiliation",
        }
    )
    assert values["business_conflict_bucket"] == "relationship_power"
    assert values["business_payoff_bucket"] == "face_slap_counter"
    assert values["business_emotion_bucket"] == "anger_humiliation"
    assert values["business_content_archetype"] in {
        "power_payoff",
        "relationship_payoff",
        "survival_suspense",
        "workplace_counter",
        "comedy_light",
        "unclear",
    }


def test_compare_llm_vs_rule() -> None:
    llm = {
        "t1": {
            "business_content_archetype": "power_payoff",
            "business_conflict_bucket": "relationship_power",
            "business_payoff_bucket": "face_slap_counter",
            "business_emotion_bucket": "anger_humiliation",
        },
        "t2": {
            "business_content_archetype": "relationship_payoff",
            "business_conflict_bucket": "relationship_power",
            "business_payoff_bucket": "none",
            "business_emotion_bucket": "neutral",
        },
    }
    rule = {
        "t1": {
            "business_content_archetype": "power_payoff",
            "business_conflict_bucket": "relationship_power",
            "business_payoff_bucket": "face_slap_counter",
            "business_emotion_bucket": "anger_humiliation",
        },
        "t2": {
            "business_content_archetype": "unclear",
            "business_conflict_bucket": "relationship_power",
            "business_payoff_bucket": "none",
            "business_emotion_bucket": "neutral",
        },
    }
    stats = compare_llm_vs_rule(llm, rule)
    assert "business_content_archetype" in stats
    assert stats["business_conflict_bucket"]["par"] >= 0.5
    assert stats["business_payoff_bucket"]["n"] == 2.0

