from service.script_tools.character_graph_builder import build_character_graph


def test_build_character_graph_filters_tool_npc_and_action_names() -> None:
    graph = build_character_graph(
        characters=[
            {
                "id": "c1",
                "name": "萧南",
                "role_in_arc": "actor",
                "archetype": "weak_start_hidden_strong",
                "arc_type": "power_growth",
                "agency_level": "high",
                "appearance_count": 30,
            },
            {
                "id": "c2",
                "name": "电话",
                "role_in_arc": "helper",
                "archetype": "tool_npc",
                "arc_type": "static",
                "agency_level": "low",
                "appearance_count": 10,
            },
            {
                "id": "c3",
                "name": "保镖做出请的手势",
                "role_in_arc": "observer",
                "archetype": "tool_npc",
                "arc_type": "static",
                "agency_level": "low",
                "appearance_count": 8,
            },
        ],
        relationships=[
            {
                "a_id": "c1",
                "b_id": "c2",
                "type": "ally",
                "polarity": "positive",
                "evidence": {"cooccurrence": 5},
            },
            {
                "a_id": "c1",
                "b_id": "c3",
                "type": "rival",
                "polarity": "negative",
                "evidence": {"cooccurrence": 3},
            },
        ],
    )

    assert [node["id"] for node in graph["nodes"]] == ["c1"]
    assert graph["edges"] == []
