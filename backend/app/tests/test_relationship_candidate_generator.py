from service.script_tools.relationship_candidate_generator import build_relationship_candidates


def test_build_relationship_candidates_threshold_and_topk() -> None:
    unit_hits = {
        "u1": {"a", "b", "c"},
        "u2": {"a", "b"},
        "u3": {"a", "b"},
        "u4": {"a", "c"},
        "u5": {"b", "c"},
        "u6": {"a", "d"},
    }
    candidates = build_relationship_candidates(unit_hits, min_cooccurrence=2, top_k=2)
    assert len(candidates) == 2
    assert candidates[0].src_char_id == "a"
    assert candidates[0].dst_char_id == "b"
    assert candidates[0].cooccurrence == 3
    assert all(c.cooccurrence >= 2 for c in candidates)


def test_build_relationship_candidates_empty_when_no_pair() -> None:
    unit_hits = {
        "u1": {"a"},
        "u2": {"b"},
        "u3": {"c"},
    }
    candidates = build_relationship_candidates(unit_hits, min_cooccurrence=1, top_k=5)
    assert candidates == []

