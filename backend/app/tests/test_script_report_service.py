import asyncio

from service import script_report_service as srs


class _ComplianceStub:
    def __init__(self) -> None:
        self.score = 0.0
        self.tier = "clean"
        self.reason = ""
        self.evidence_ref_ids: list[str] = []

    def to_dict(self) -> dict:
        return {"status": "pass", "hits": []}


def test_generate_report_runs_full_tag_pipeline(monkeypatch) -> None:
    calls: list[dict] = []

    async def _fake_run_tag_pipeline(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(
        srs.progress_tracker,
        "start",
        lambda _script_id: None,
    )
    monkeypatch.setattr(
        srs.progress_tracker,
        "update_stage",
        lambda _script_id, _stage_id, _state, detail=None: None,
    )
    monkeypatch.setattr(
        srs.progress_tracker,
        "finalize",
        lambda _script_id, error=None: None,
    )
    monkeypatch.setattr(
        srs,
        "_load_script_meta",
        lambda script_id, engine=None: srs._ScriptMeta(
            script_id=script_id,
            title="demo",
            total_episodes=1,
            total_scenes=2,
        ),
    )
    monkeypatch.setattr(
        srs,
        "run_tag_pipeline",
        _fake_run_tag_pipeline,
    )
    monkeypatch.setattr(
        srs,
        "load_rubric",
        lambda _version: type(
            "R",
            (),
            {
                "rubric_id": "v3.0.0",
                "score_ver": "v3.0.0",
                "base_weight": {},
                "llm_bundles": [],
            },
        )(),
    )
    monkeypatch.setattr(
        srs,
        "build_signal_context",
        lambda script_id, engine=None: type(
            "C",
            (),
            {
                "drama_tags": ["都市言情"],
                "plot_unit_count": 1,
                "episode_count": 1,
                "tag_set_ver": "script",
            },
        )(),
    )

    async def _fake_compute_signals(*args, **kwargs):
        return {}

    monkeypatch.setattr(srs, "compute_signals", _fake_compute_signals)
    monkeypatch.setattr(
        srs,
        "aggregate",
        lambda _rubric, _signals: [
            srs.DimensionScore(
                dimension="story",
                score=6.2,
                coverage_ratio=0.9,
                confidence="high",
                tier="good",
                signal_refs=[
                    {
                        "signal_key": "opening_speed",
                        "score": 6.2,
                        "value": {"opening_index": 1},
                        "source": "rule",
                        "confidence": 0.8,
                        "weight_in_dim": 0.5,
                        "evidence_refs": [{"scene_id": "scene-1"}],
                    }
                ],
                top_signals=[
                    {
                        "signal_key": "opening_speed",
                        "value": {"opening_index": 1},
                        "score": 6.2,
                        "weight_in_dim": 0.5,
                        "source": "rule",
                        "confidence": 0.8,
                        "contribution": 3.1,
                    }
                ],
                tier_cuts={"p25": 4.0, "p50": 6.0, "p75": 8.0},
                reason="story weighted aggregation",
            )
        ],
    )
    monkeypatch.setattr(
        srs,
        "apply_genre_weights",
        lambda _rubric, _dim_scores, genre_scope=None: type("W", (), {"overall_score": 0.0})(),
    )
    monkeypatch.setattr(srs, "infer_genre_scope", lambda _tags: "default")
    async def _fake_screen_compliance(script_id, caller=None):  # noqa: ANN001
        return _ComplianceStub()

    monkeypatch.setattr(srs, "screen_compliance", _fake_screen_compliance)
    monkeypatch.setattr(
        srs,
        "decide",
        lambda _dim_scores, _weighted, compliance=None: type(
            "Decision",
            (),
            {
                "decision": "recommended",
                "confidence": "high",
                "one_sentence_reason": "ok",
                "payload": {},
            },
        )(),
    )
    monkeypatch.setattr(
        srs,
        "generate_actions",
        lambda **kwargs: [
            srs.ImprovementAction(
                id="act-1",
                run_id="run-1",
                script_id=kwargs["script_id"],
                dimension="story",
                signal_key="opening_speed",
                template_id="opening_speed:v1",
                issue="开场冲突不足",
                target="首场20段内给冲突",
                action_steps=["压缩铺垫", "提前冲突"],
                evidence_refs=[{"scene_id": "scene-1"}],
                estimated_lift={"story": 0.8},
            )
        ],
    )
    monkeypatch.setattr(srs, "aggregate_pacing_curve", lambda _ctx: [])
    monkeypatch.setattr(srs, "_persist_report", lambda **kwargs: None)
    monkeypatch.setattr(srs, "_mark_script_status", lambda **kwargs: None)

    async def _fake_extract_reward_events(*, script_id, caller=None, max_scenes=200):
        return []

    async def _fake_extract_coverage_card(*, script_id, caller=None, engine=None, max_scenes=18):
        return srs.CoverageCard(
            logline="主角逆袭打脸全场",
            recommendation="recommend",
            confidence="medium",
            genre=["都市"],
            core_value="爽点密集",
            strengths=[],
            concerns=[],
        )

    async def _fake_extract_beat_sheet(*, script_id, reward_events=None, caller=None, engine=None):
        from service.script_tools.beat_chain import BeatAct, BeatNode
        return srs.BeatSheet(
            acts=[
                BeatAct(
                    act=1,
                    title="开局",
                    scene_range=[],
                    beats=[
                        BeatNode(type="opening", summary="主角登场", anchor_scene_id="scene-1"),
                    ],
                )
            ]
        )

    async def _fake_extract_character_graph(*, script_id, caller=None, engine=None, max_nodes=12, max_edges=30):
        from service.script_tools.character_graph_chain import CharacterEdge, CharacterNode
        return srs.CharacterGraph(
            nodes=[
                CharacterNode(id="c1", name="主角", role="protagonist", appearance_count=5),
                CharacterNode(id="c2", name="反派", role="antagonist", appearance_count=3),
            ],
            edges=[
                CharacterEdge(source_id="c1", target_id="c2", type="rival", weight=0.6, polarity="negative"),
            ],
        )

    monkeypatch.setattr(srs, "extract_reward_events", _fake_extract_reward_events)
    monkeypatch.setattr(srs, "extract_coverage_card", _fake_extract_coverage_card)
    monkeypatch.setattr(srs, "extract_beat_sheet", _fake_extract_beat_sheet)
    monkeypatch.setattr(srs, "extract_character_graph", _fake_extract_character_graph)
    monkeypatch.setattr(srs, "_load_drama_tags", lambda script_id, engine=None: [{"key": "drama_tags", "value": "都市", "confidence": 0.8}])
    monkeypatch.setattr(
        srs,
        "_load_plot_units",
        lambda script_id, engine=None: [
            {
                "plot_unit_id": "pu-1",
                "episode_no": 1,
                "plot_unit_no": 1,
                "summary": "开场冲突",
                "scene_refs": ["scene-1"],
                "narrative_intensity": 6,
                "plot_hook": "identity_reveal",
                "conflict_type": "revenge",
                "payoff_type": "none",
                "emotional_driver": "anger",
                "story_stage": "opening",
            }
        ],
    )
    monkeypatch.setattr(
        srs,
        "_load_characters",
        lambda script_id, engine=None: [
            {
                "id": "c1",
                "name": "主角",
                "aliases": ["阿主"],
                "archetype": "hero",
                "role_in_arc": "lead",
                "arc_type": "growth",
                "agency_level": "high",
                "appearance_count": 5,
            }
        ],
    )
    monkeypatch.setattr(
        srs,
        "_load_character_relationships",
        lambda script_id, engine=None: [
            {
                "id": "r1",
                "a_id": "c1",
                "b_id": "c2",
                "type": "rival",
                "polarity": "negative",
                "dynamic_arc": "escalating",
                "triangle": "",
            }
        ],
    )

    report = asyncio.run(srs.generate_report(script_id="sid-1"))

    assert len(calls) == 1
    assert calls[0]["script_ref"] == "sid-1"
    assert calls[0]["tag_set_ver"] == "script"
    assert report["drama_tags"]
    assert report["plot_units"]
    assert report["characters"]
    assert report["character_relationships"]
    assert report["coverage_card"]["logline"] == "主角逆袭打脸全场"
    assert report["coverage_card"]["recommendation"] == "recommend"
    assert report["beat_sheet"]["acts"][0]["beats"][0]["type"] == "opening"
    assert report["character_graph"]["nodes"]
    assert report["character_graph"]["edges"]
    assert report["character_graph"]["nodes"][0]["role"] == "protagonist"
    assert report["must_read_scene_ids"] == ["scene-1"]
    assert len(report["highlights"]) == 1
    assert report["highlights"][0]["type"] == "hook"
    assert report["highlights"][0]["scene_id"] == "scene-1"
    assert report["evidence_refs"] == []
    assert report["risk_flags"] == []
    assert report["evaluation"]["dimensions"][0]["top_signals"]
    assert report["evaluation"]["dimensions"][0]["tier_cuts"]["p50"] == 6.0
    assert report["decision"]["decision_inputs"]["tier_cuts_used"]["story"]["p75"] == 8.0
    assert report["decision"]["decision_inputs"]["overall_cuts"]["p25"] == 4.0
