from service import script_report_service as srs


def test_build_evidence_refs_minimal_merges_reward_and_risk() -> None:
    from service.script_tools.reward_extractor import RewardEvent

    reward_events = [
        RewardEvent(
            scene_id="scene-a",
            scene_no="1-1",
            episode_no=1,
            event_type="reversal",
            claim="主角揭穿身份反转",
            quote_verbatim="主角揭穿身份",
            quote_verified=True,
            evidence_line_range=(3, 5),
        ),
        # duplicate (scene_id, line_range) should be skipped
        RewardEvent(
            scene_id="scene-a",
            scene_no="1-1",
            episode_no=1,
            event_type="face_slap",
            claim="紧接着的连续打脸",
            quote_verbatim="紧接着的连续打脸",
            quote_verified=True,
            evidence_line_range=(3, 5),
        ),
        RewardEvent(
            scene_id="scene-b",
            scene_no="2-2",
            episode_no=2,
            event_type="cp_progress",
            claim="CP 牵手",
            quote_verbatim="CP 牵手",
            quote_verified=True,
            evidence_line_range=(10, 12),
        ),
    ]
    compliance_hits = [
        {
            "scene_id": "scene-c",
            "scene_no": "3-1",
            "episode_no": 3,
            "level": "high_risk",
            "category": "violent_revenge",
            "excerpt": "极端复仇片段",
            "evidence_line_range": [4, 6],
        },
        # missing scene_id => filtered
        {"scene_id": "", "excerpt": "x", "category": "noise"},
    ]
    refs = srs._build_evidence_refs_minimal(reward_events, compliance_hits)
    assert len(refs) == 3

    reversal_ref = next(r for r in refs if r["scene_id"] == "scene-a")
    assert reversal_ref["id"] == "evi_reward_scene-a_reversal"
    assert reversal_ref["start_line"] == 3
    assert reversal_ref["end_line"] == 5
    assert reversal_ref["quote_source"] == "reward:reversal"
    assert reversal_ref["confidence"] == "high"

    cp_ref = next(r for r in refs if r["scene_id"] == "scene-b")
    assert cp_ref["id"] == "evi_reward_scene-b_cp_progress"
    assert cp_ref["confidence"] == "medium"

    risk_ref = next(r for r in refs if r["scene_id"] == "scene-c")
    assert risk_ref["id"].startswith("evi_risk_scene-c_")
    assert risk_ref["quote_source"] == "risk_hit"
    assert risk_ref["confidence"] == "high"
    assert risk_ref["start_line"] == 4

    # highlights should reuse evidence_refs ids (same id-space contract)
    highlights = srs._build_highlights_minimal(reward_events, None, refs)
    assert highlights, "expected highlight for reward events"
    by_scene = {h["scene_id"]: h for h in highlights}
    assert by_scene["scene-a"]["id"] == "evi_reward_scene-a_reversal"
    assert by_scene["scene-b"]["id"] == "evi_reward_scene-b_cp_progress"
